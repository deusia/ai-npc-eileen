import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app

class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = patch.object(app, 'DB', Path(self.temp.name) / 'test.db')
        self.db.start()
        app.init()
    def tearDown(self):
        self.db.stop()
        self.temp.cleanup()
    def test_quote_confirmation_and_replay(self):
        with app.connect() as c:
            q = app.execute_tool(c, 'propose_room', {'days':3,'discount_percent':10})
            self.assertEqual(q['price'],54)
        self.assertEqual(app.confirm(q['quote_id'])['world'], {'rooms':1,'coins':46})
        with self.assertRaises(ValueError): app.confirm(q['quote_id'])
    def test_invalid_quote_and_insufficient_coins(self):
        with app.connect() as c:
            self.assertIn('error', app.execute_tool(c, 'propose_room', {'days':True,'discount_percent':0}))
            q = app.execute_tool(c, 'propose_room', {'days':7,'discount_percent':0})
        with self.assertRaises(ValueError): app.confirm(q['quote_id'])
        with app.connect() as c:
            self.assertEqual(app.state(c)['world'], {'rooms':2,'coins':100})
    def test_agent_tool_and_rollback(self):
        call = {'choices':[{'message':{'role':'assistant','tool_calls':[{'id':'1','type':'function','function':{'name':'remember','arguments':'{"memory":"玩家喜欢茶"}'}}]}}]}
        answer = {'choices':[{'message':{'role':'assistant','content':'请坐。'}}]}
        with patch.object(app, 'model_id', return_value='test'), patch.object(app, 'lm', side_effect=[call, answer]):
            self.assertEqual(app.chat('你好')['reply'], '请坐。')
        with app.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM memories').fetchone()[0],1)
        call['choices'][0]['message']['tool_calls'][0]['function']['arguments'] = '{"memory":"玩家喜欢咖啡"}'
        with patch.object(app, 'model_id', return_value='test'), patch.object(app, 'lm', side_effect=[call, RuntimeError('失败')]):
            with self.assertRaises(RuntimeError): app.chat('再见')
        with app.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM messages').fetchone()[0],2)
            self.assertEqual(c.execute('SELECT count(*) FROM memories').fetchone()[0],1)

    def test_sessions_isolate_memory_quotes_and_world(self):
        with app.connect() as c:
            second = app.create_session(c)
            app.execute_tool(c, 'remember', {'memory':'喜欢茶'}, 1)
            app.execute_tool(c, 'remember', {'memory':'喜欢茶'}, second)
            quote = app.execute_tool(c, 'propose_room', {'days':2,'discount_percent':0}, 1)
            self.assertEqual(c.execute('SELECT count(*) FROM memories').fetchone()[0], 2)
            self.assertIsNone(app.state(c, second)['quote'])
        with self.assertRaises(ValueError):
            app.confirm(quote['quote_id'], second)
        app.confirm(quote['quote_id'], 1)
        with app.connect() as c:
            self.assertEqual(app.state(c, second)['world'], {'rooms':2,'coins':100})
            self.assertEqual(app.state(c, 1)['world'], {'rooms':1,'coins':60})

    def test_model_context_does_not_leak_other_session(self):
        with app.connect() as c:
            second = app.create_session(c)
            c.execute("INSERT INTO messages(role, content, session_id) VALUES ('user', '旧会话的秘密', 1)")
            app.execute_tool(c, 'remember', {'memory':'旧会话喜欢茶'}, 1)
        answer = {'choices':[{'message':{'role':'assistant','content':'欢迎。'}}]}
        with patch.object(app, 'model_id', return_value='test'), patch.object(app, 'lm', return_value=answer) as model:
            app.chat('初次见面', second)
        messages = model.call_args.args[1]['messages']
        self.assertNotIn('旧会话的秘密', str(messages))
        self.assertNotIn('旧会话喜欢茶', str(messages))
        self.assertEqual(messages[-1]['content'], '初次见面')

    def test_story_perception_actions_and_legacy_isolation(self):
        with app.connect() as c:
            first = app.create_session(c)
            second = app.create_session(c)
            self.assertIsNone(app.story_perception(c, 1))
            self.assertIsNone(app.state(c, 1)['opening'])
            self.assertIn('旧灯', app.state(c, first)['opening'])
            self.assertTrue(app.story_perception(c, first)['本次感知'].startswith('旧灯忽然'))
            self.assertIn('柜台', app.execute_tool(c, 'inspect_surroundings', {'target':'lantern'}, first)['result'])
            self.assertIn('error', app.execute_tool(c, 'move_lantern', {'destination':'outside'}, first))
            self.assertIn('窗边', app.execute_tool(c, 'move_lantern', {'destination':'window'}, first)['result'])
            self.assertEqual(app.story_perception(c, second)['旧灯位置'], '柜台')
        answer = {'choices':[{'message':{'role':'assistant','content':'谁在那里？'}}]}
        with patch.object(app, 'model_id', return_value='test'), patch.object(app, 'lm', return_value=answer) as model:
            app.chat('你能听见我吗？', first)
        context = model.call_args.args[1]['messages'][0]['content']
        self.assertIn('窗边', context)
        self.assertIn('来者位置和外貌', context)
        self.assertNotIn('点击网页', context)
        with app.connect() as c:
            self.assertIn('再次', app.story_perception(c, first)['本次感知'])
            self.assertIsNone(app.state(c, first)['opening'])
            self.assertEqual(c.execute('SELECT count(*) FROM events WHERE session_id=? AND content LIKE ?', (first, '%第一次%')).fetchone()[0],1)

    def test_story_first_encounter_rolls_back_on_failure(self):
        with app.connect() as c:
            session = app.create_session(c)
        with patch.object(app, 'model_id', return_value='test'), patch.object(app, 'lm', side_effect=RuntimeError('失败')):
            with self.assertRaises(RuntimeError): app.chat('你好', session)
        with app.connect() as c:
            self.assertIn('旧灯忽然', app.story_perception(c, session)['本次感知'])
            self.assertEqual(c.execute('SELECT count(*) FROM events WHERE session_id=?', (session,)).fetchone()[0],0)

    def test_lantern_clue_is_hidden_until_inspected_and_is_session_scoped(self):
        with app.connect() as c:
            first = app.create_session(c)
            second = app.create_session(c)
            self.assertNotIn('已观察到的旧灯细节', app.story_perception(c, first))
            overview = app.execute_tool(c, 'inspect_surroundings', {'target':'lantern'}, first)
            self.assertNotIn('刻痕', overview['result'])
            self.assertIn('发现', app.execute_tool(c, 'inspect_surroundings', {'target':'lantern_base'}, first)['result'])
            self.assertIn('再次', app.execute_tool(c, 'inspect_surroundings', {'target':'lantern_base'}, first)['result'])
        app.init()
        with app.connect() as c:
            clue = app.story_perception(c, first)['已观察到的旧灯细节']
            self.assertIn('含义尚不清楚', clue)
            self.assertNotIn('已观察到的旧灯细节', app.story_perception(c, second))
            self.assertEqual(c.execute("SELECT count(*) FROM events WHERE session_id=? AND content LIKE '%发现灯座%'", (first,)).fetchone()[0], 1)

    def test_clue_discovery_rolls_back_on_model_failure(self):
        with app.connect() as c:
            session = app.create_session(c)
        tool_call = {'choices':[{'message':{'role':'assistant','tool_calls':[{'id':'1','function':{'name':'inspect_surroundings','arguments':'{\"target\":\"lantern_base\"}'}}]}}]}
        with patch.object(app, 'model_id', return_value='test'), patch.object(app, 'lm', side_effect=[tool_call, RuntimeError('失败')]):
            with self.assertRaises(RuntimeError): app.chat('仔细看看灯座',session)
        with app.connect() as c:
            self.assertNotIn('已观察到的旧灯细节', app.story_perception(c,session))
            self.assertEqual(c.execute('SELECT count(*) FROM events WHERE session_id=?',(session,)).fetchone()[0],0)

    def test_relevant_memory_can_return_beyond_recent_twenty(self):
        with app.connect() as c:
            app.create_session(c)
            c.execute("INSERT INTO memories(content,session_id) VALUES ('玩家提到喜欢茉莉花茶',1)")
            for i in range(30):
                c.execute('INSERT INTO memories(content,session_id) VALUES (?,1)', (f'普通记录 {i}',))
            c.execute("INSERT INTO memories(content,session_id) VALUES ('其他会话的茉莉花茶',2)")
            memories = [m['content'] for m in app.relevant_memories(c, 1, '我之前说喜欢茉莉花茶，你还记得吗？')]
        self.assertIn('玩家提到喜欢茉莉花茶', memories)
        self.assertNotIn('其他会话的茉莉花茶', memories)
        self.assertEqual(len(memories), 20)
        self.assertIn('普通记录 29', memories)

    def test_story_memory_revision_retains_history_and_isolation(self):
        with app.connect() as c:
            one = app.create_session(c)
            two = app.create_session(c)
            item = app.execute_tool(c, 'remember', {'memory':'声音可能来自门外','kind':'belief'}, one)
            mid = item['memory_id']
            self.assertIn('error', app.execute_tool(c, 'revise_memory', {'memory_id':mid,'memory':'来自灯里','kind':'belief','reason':'新观察'}, two))
            self.assertIn('error', app.execute_tool(c, 'revise_memory', {'memory_id':mid,'memory':'来自灯里','kind':'belief','reason':''}, one))
            revised = app.execute_tool(c, 'revise_memory', {'memory_id':mid,'memory':'声音也许来自旧灯','kind':'belief','reason':'查看门窗后仍有声音'}, one)
            self.assertEqual(revised['memory_id'], mid)
            current = c.execute('SELECT content,kind FROM memories WHERE id=?', (mid,)).fetchone()
            history = c.execute('SELECT content,kind,reason FROM memory_history WHERE memory_id=?', (mid,)).fetchone()
            self.assertEqual(tuple(current), ('声音也许来自旧灯','belief'))
            self.assertEqual(tuple(history), ('声音可能来自门外','belief','查看门窗后仍有声音'))
            self.assertEqual(app.relevant_memories(c, one, '旧灯')[0]['id'], mid)

    def test_memory_revision_rolls_back_if_reply_fails(self):
        with app.connect() as c:
            session = app.create_session(c)
            mid = app.execute_tool(c, 'remember', {'memory':'声音来自门外','kind':'belief'}, session)['memory_id']
        tool_call = {'choices':[{'message':{'role':'assistant','tool_calls':[{'id':'1','function':{'name':'revise_memory','arguments':json.dumps({'memory_id':mid,'memory':'声音可能来自旧灯','kind':'belief','reason':'检查门窗后'})}}]}}]}
        with patch.object(app, 'model_id', return_value='test'), patch.object(app, 'lm', side_effect=[tool_call, RuntimeError('失败')]):
            with self.assertRaises(RuntimeError): app.chat('你刚才发现了什么？',session)
        with app.connect() as c:
            self.assertEqual(c.execute('SELECT content FROM memories WHERE id=?',(mid,)).fetchone()[0], '声音来自门外')
            self.assertEqual(c.execute('SELECT count(*) FROM memory_history WHERE memory_id=?',(mid,)).fetchone()[0],0)
            self.assertEqual(c.execute('SELECT voices_heard FROM story_world WHERE session_id=?',(session,)).fetchone()[0],0)

    def test_world_clock_advances_between_visits_and_stays_isolated(self):
        with app.connect() as c:
            first = app.create_session(c)
            second = app.create_session(c)
            self.assertEqual(app.state(c, first, 1000)['story_time'], {'day':1,'hour':21,'minute':0,'period':'夜晚'})
        answer = {'choices':[{'message':{'role':'assistant','content':'我听见了。'}}]}
        with patch.object(app, 'model_id', return_value='test'), patch.object(app, 'lm', return_value=answer) as model:
            with patch.object(app.time, 'time', return_value=1000):
                app.chat('你好', first)
            with patch.object(app.time, 'time', return_value=1000 + 8 * 3600):
                app.chat('我回来了', first)
            context = model.call_args.args[1]['messages'][0]['content']
        self.assertIn('第2天 清晨 05:00', context)
        self.assertIn('约480分钟', context)
        app.init()
        with app.connect() as c:
            self.assertEqual(app.state(c, first, 1000 + 8 * 3600)['story_time'], {'day':2,'hour':5,'minute':0,'period':'清晨'})
            self.assertEqual(app.state(c, second, 1000 + 8 * 3600)['story_time']['hour'],21)

    def test_daily_routine_respects_actions_and_updates_after_time_passes(self):
        with app.connect() as c:
            session = app.create_session(c)
            other = app.create_session(c)
        answer = {'choices':[{'message':{'role':'assistant','content':'我听见了。'}}]}
        with patch.object(app, 'model_id', return_value='test'), patch.object(app, 'lm', return_value=answer) as model:
            with patch.object(app.time, 'time', return_value=1000):
                app.chat('你好',session)
            with app.connect() as c:
                app.execute_tool(c, 'move_lantern', {'destination':'window'}, session)
            with patch.object(app.time, 'time', return_value=1060):
                app.chat('你在哪里？',session)
            context = model.call_args.args[1]['messages'][0]['content']
            self.assertIn('仍在窗边',context)
            with patch.object(app.time, 'time', return_value=1000+8*3600):
                app.chat('我回来了',session)
                app.chat('你在忙吗？',session)
            context = model.call_args.args[1]['messages'][0]['content']
            self.assertIn('检查旅店存货',context)
        app.init()
        with app.connect() as c:
            story = c.execute('SELECT * FROM story_world WHERE session_id=?',(session,)).fetchone()
            self.assertEqual(story['npc_location'],'counter')
            self.assertEqual(story['lantern_location'],'window')
            self.assertEqual(c.execute("SELECT count(*) FROM events WHERE session_id=? AND content LIKE '%中间未记录的经历未知%'",(session,)).fetchone()[0],1)
            self.assertEqual(c.execute('SELECT count(*) FROM events WHERE session_id=?',(other,)).fetchone()[0],0)

    def test_daily_routine_change_rolls_back_when_reply_fails(self):
        with app.connect() as c:
            session = app.create_session(c)
        answer = {'choices':[{'message':{'role':'assistant','content':'你好。'}}]}
        with patch.object(app, 'model_id', return_value='test'):
            with patch.object(app.time, 'time', return_value=1000), patch.object(app, 'lm', return_value=answer):
                app.chat('你好',session)
            with patch.object(app.time, 'time', return_value=1000+8*3600), patch.object(app, 'lm', side_effect=RuntimeError('失败')):
                with self.assertRaises(RuntimeError): app.chat('回来',session)
        with app.connect() as c:
            row=c.execute('SELECT routine_code,routine_day FROM story_world WHERE session_id=?',(session,)).fetchone()
            self.assertEqual(tuple(row),('ledger',1))
            self.assertEqual(c.execute("SELECT count(*) FROM events WHERE session_id=? AND content LIKE '%中间未记录的经历未知%'",(session,)).fetchone()[0],0)

    def test_away_event_and_unopened_note_are_isolated_and_idempotent(self):
        with app.connect() as c:
            first=app.create_session(c)
            other=app.create_session(c)
        answer={'choices':[{'message':{'role':'assistant','content':'我在这里。'}}]}
        with patch.object(app,'model_id',return_value='test'), patch.object(app,'lm',return_value=answer) as model:
            with patch.object(app.time,'time',return_value=1000): app.chat('你好',first)
            with app.connect() as c:
                app.sync_story_events(c,first,4599)
                self.assertNotIn('门边纸条',app.story_perception(c,first,4599))
                self.assertIn('error',app.execute_tool(c,'read_note',{},first))
            with patch.object(app.time,'time',return_value=4600): app.chat('我回来了',first)
            context=model.call_args.args[1]['messages'][0]['content']
            self.assertIn('尚未打开',context)
            self.assertNotIn(app.NOTE_TEXT,context)
            with patch.object(app.time,'time',return_value=4600): app.chat('你在吗？',first)
        with app.connect() as c:
            self.assertNotIn('门边纸条',app.story_perception(c,other,4600))
            self.assertEqual(c.execute("SELECT count(*) FROM events WHERE session_id=? AND content LIKE '%折起的纸条%'",(first,)).fetchone()[0],1)
            self.assertIn(app.NOTE_TEXT,app.execute_tool(c,'read_note',{},first)['result'])
            self.assertIn(app.NOTE_TEXT,app.story_perception(c,first,4600)['已读纸条'])
            app.execute_tool(c,'read_note',{},first)
            self.assertEqual(c.execute("SELECT count(*) FROM events WHERE session_id=? AND content LIKE '%打开门边%'",(first,)).fetchone()[0],1)
        app.init()
        with app.connect() as c:
            self.assertIn(app.NOTE_TEXT,app.story_perception(c,first,4600)['已读纸条'])

    def test_away_event_rolls_back_if_reply_fails(self):
        with app.connect() as c: session=app.create_session(c)
        answer={'choices':[{'message':{'role':'assistant','content':'你好。'}}]}
        with patch.object(app,'model_id',return_value='test'):
            with patch.object(app.time,'time',return_value=1000),patch.object(app,'lm',return_value=answer): app.chat('你好',session)
            with patch.object(app.time,'time',return_value=4600),patch.object(app,'lm',side_effect=RuntimeError('失败')):
                with self.assertRaises(RuntimeError): app.chat('回来了',session)
        with app.connect() as c:
            row=c.execute('SELECT note_state,last_contact_at FROM story_world WHERE session_id=?',(session,)).fetchone()
            self.assertEqual(tuple(row),('waiting',1000))
            self.assertEqual(c.execute("SELECT count(*) FROM events WHERE session_id=? AND content LIKE '%折起的纸条%'",(session,)).fetchone()[0],0)

    def test_story_worker_runs_and_can_stop(self):
        ran = threading.Event()
        with patch.object(app,'background_story_tick',side_effect=ran.set):
            stop = app.start_story_worker()
            self.assertTrue(ran.wait(2))
            stop.set()

    def test_background_world_event_and_autonomous_action_only_once(self):
        with app.connect() as c:
            sid=app.create_session(c)
            c.execute("UPDATE story_world SET voices_heard=1,clock_started_at=1000,last_contact_at=1000 WHERE session_id=?",(sid,))
        reply={'choices':[{'finish_reason':'tool_calls','message':{'tool_calls':[{'function':{'name':'read_note','arguments':'{}'}}]}}]}
        with patch.object(app,'model_id',return_value='test'),patch.object(app,'lm',return_value=reply) as model:
            app.background_story_tick(4599)
            self.assertEqual(model.call_count,0)
            app.background_story_tick(4600)
            app.background_story_tick(4601)
            self.assertEqual(model.call_count,1)
            payload=model.call_args.args[1]
            self.assertNotIn(app.NOTE_TEXT, str(payload['messages']))
            self.assertEqual(len(payload['messages']),2)
            self.assertIn('世界事件',payload['messages'][1]['content'])
        app.init()
        with app.connect() as c:
            row=c.execute('SELECT note_state,note_reaction FROM story_world WHERE session_id=?',(sid,)).fetchone()
            self.assertEqual(tuple(row),('read','done'))
            self.assertEqual(c.execute('SELECT count(*) FROM messages WHERE session_id=?',(sid,)).fetchone()[0],0)
            self.assertEqual(c.execute("SELECT count(*) FROM events WHERE session_id=? AND content LIKE '%打开门边%'",(sid,)).fetchone()[0],1)

    def test_background_can_choose_no_action(self):
        with app.connect() as c:
            sid=app.create_session(c)
            c.execute("UPDATE story_world SET voices_heard=1,clock_started_at=1000,last_contact_at=1000 WHERE session_id=?",(sid,))
        reply={'choices':[{'finish_reason':'stop','message':{'content':'先不动它。'}}]}
        with patch.object(app,'model_id',return_value='test'),patch.object(app,'lm',return_value=reply): app.background_story_tick(4600)
        with app.connect() as c:
            row=c.execute('SELECT note_state,note_reaction FROM story_world WHERE session_id=?',(sid,)).fetchone()
            self.assertEqual(tuple(row),('unopened','done'))
            self.assertEqual(c.execute("SELECT count(*) FROM events WHERE session_id=? AND content LIKE '%打开门边%'",(sid,)).fetchone()[0],0)

    def test_background_discards_stale_decision_after_new_message(self):
        with app.connect() as c:
            sid=app.create_session(c)
            c.execute("UPDATE story_world SET voices_heard=1,clock_started_at=1000,last_contact_at=1000 WHERE session_id=?",(sid,))
        def model(*args):
            with app.connect() as c:
                c.execute("INSERT INTO messages(role,content,session_id) VALUES ('user','请先别打开',?)",(sid,))
            return {'choices':[{'finish_reason':'tool_calls','message':{'tool_calls':[{'function':{'name':'read_note','arguments':'{}'}}]}}]}
        with patch.object(app,'model_id',return_value='test'),patch.object(app,'lm',side_effect=model): app.background_story_tick(4600)
        with app.connect() as c:
            row=c.execute('SELECT note_state,note_reaction FROM story_world WHERE session_id=?',(sid,)).fetchone()
            self.assertEqual(tuple(row),('unopened','stale'))
            self.assertEqual(c.execute("SELECT count(*) FROM events WHERE session_id=? AND content LIKE '%打开门边%'",(sid,)).fetchone()[0],0)

    def test_background_failure_does_not_retry_after_intervening_message(self):
        with app.connect() as c:
            sid=app.create_session(c)
            c.execute("UPDATE story_world SET voices_heard=1,clock_started_at=1000,last_contact_at=1000 WHERE session_id=?",(sid,))
        def model(*args):
            with app.connect() as c:
                c.execute("INSERT INTO messages(role,content,session_id) VALUES ('user','我回来了',?)",(sid,))
            raise RuntimeError('离线')
        with patch.object(app,'model_id',return_value='test'),patch.object(app,'lm',side_effect=model) as inference:
            with self.assertLogs(app.LOG,level='WARNING'):
                app.background_story_tick(4600)
                app.background_story_tick(4900)
            self.assertEqual(inference.call_count,1)
        with app.connect() as c:
            row=c.execute('SELECT note_state,note_reaction,note_attempts FROM story_world WHERE session_id=?',(sid,)).fetchone()
            self.assertEqual(tuple(row),('unopened','stale',0))

    def test_background_inference_allows_player_to_return(self):
        with app.connect() as c:
            sid=app.create_session(c)
            c.execute("UPDATE story_world SET voices_heard=1,clock_started_at=1000,last_contact_at=1000 WHERE session_id=?",(sid,))
        started=threading.Event(); release=threading.Event()
        def model(path, body):
            if body['messages'][-1]['content'].startswith('【世界事件】'):
                started.set()
                if not release.wait(3): raise RuntimeError('测试超时')
                return {'choices':[{'finish_reason':'tool_calls','message':{'tool_calls':[{'function':{'name':'read_note','arguments':'{}'}}]}}]}
            return {'choices':[{'message':{'content':'我在。'}}]}
        with patch.object(app,'model_id',return_value='test'),patch.object(app,'lm',side_effect=model):
            worker=threading.Thread(target=app.background_story_tick,args=(4600,))
            worker.start()
            try:
                self.assertTrue(started.wait(2))
                with patch.object(app.time,'time',return_value=4601):
                    self.assertEqual(app.chat('我回来了',sid)['reply'],'我在。')
            finally:
                release.set();worker.join(3)
        self.assertFalse(worker.is_alive())
        with app.connect() as c:
            row=c.execute('SELECT note_state,note_reaction FROM story_world WHERE session_id=?',(sid,)).fetchone()
            self.assertEqual(tuple(row),('unopened','stale'))

    def test_background_failures_are_bounded_and_event_remains(self):
        with app.connect() as c:
            sid=app.create_session(c)
            c.execute("UPDATE story_world SET voices_heard=1,clock_started_at=1000,last_contact_at=1000 WHERE session_id=?",(sid,))
        with patch.object(app,'model_id',return_value='test'),patch.object(app,'lm',side_effect=RuntimeError('离线')) as model:
            with self.assertLogs(app.LOG,level='WARNING'):
                for now in (4600,4601,4900,5200,5500): app.background_story_tick(now)
            self.assertEqual(model.call_count,3)
        with app.connect() as c:
            row=c.execute('SELECT note_state,note_reaction,note_attempts FROM story_world WHERE session_id=?',(sid,)).fetchone()
            self.assertEqual(tuple(row),('unopened','failed',3))
            self.assertEqual(c.execute("SELECT count(*) FROM events WHERE session_id=? AND content LIKE '%折起的纸条%'",(sid,)).fetchone()[0],1)

    def test_world_clock_rolls_back_and_old_sessions_have_unknown_gap(self):
        with app.connect() as c:
            old = app.create_session(c)
            c.execute('UPDATE story_world SET voices_heard=3 WHERE session_id=?',(old,))
            self.assertEqual(app.story_perception(c, old, 1000)['距上次来声'],'此前已有联系，具体间隔未知')
        with patch.object(app.time, 'time', return_value=1000), patch.object(app, 'model_id', return_value='test'), patch.object(app, 'lm', side_effect=RuntimeError('失败')):
            with self.assertRaises(RuntimeError): app.chat('你好',old)
        with app.connect() as c:
            row = c.execute('SELECT clock_started_at,last_contact_at,voices_heard FROM story_world WHERE session_id=?',(old,)).fetchone()
            self.assertEqual(tuple(row),(None,None,3))

    def test_intention_completes_only_on_matching_world_action(self):
        with app.connect() as c:
            one=app.create_session(c); other=app.create_session(c)
            self.assertIn('error',app.execute_tool(c,'set_intention',{'target':'read_note','reason':'想看看'},one))
            plan=app.execute_tool(c,'set_intention',{'target':'inspect_lantern_base','reason':'想弄清旧灯'},one)
            again=app.execute_tool(c,'set_intention',{'target':'inspect_lantern_base','reason':'好奇'},one)
            self.assertEqual(plan['intention_id'],again['intention_id'])
            self.assertIn('error',app.execute_tool(c,'cancel_intention',{'intention_id':plan['intention_id'],'reason':'改变主意'},other))
            app.execute_tool(c,'inspect_surroundings',{'target':'door'},one)
            self.assertEqual(c.execute('SELECT status FROM intentions WHERE id=?',(plan['intention_id'],)).fetchone()[0],'active')
            app.execute_tool(c,'inspect_surroundings',{'target':'lantern_base'},one)
            row=c.execute('SELECT status,completed_event_id FROM intentions WHERE id=?',(plan['intention_id'],)).fetchone()
            self.assertEqual(row['status'],'completed')
            self.assertIn('灯座',c.execute('SELECT content FROM events WHERE id=? AND session_id=?',(row['completed_event_id'],one)).fetchone()[0])
        app.init()
        with app.connect() as c:
            self.assertEqual(c.execute('SELECT status FROM intentions WHERE id=?',(plan['intention_id'],)).fetchone()[0],'completed')
            self.assertEqual(c.execute('SELECT count(*) FROM intentions WHERE session_id=?',(other,)).fetchone()[0],0)

    def test_intention_cancellation_and_failed_turn_roll_back(self):
        with app.connect() as c:
            sid=app.create_session(c)
            plan=app.execute_tool(c,'set_intention',{'target':'move_lantern_to_window','reason':'想观察雨'},sid)
            self.assertEqual(app.execute_tool(c,'cancel_intention',{'intention_id':plan['intention_id'],'reason':'改变主意'},sid)['result'],'已放弃该打算')
            self.assertEqual(c.execute('SELECT status FROM intentions WHERE id=?',(plan['intention_id'],)).fetchone()[0],'abandoned')
        call={'choices':[{'message':{'role':'assistant','tool_calls':[{'id':'1','function':{'name':'set_intention','arguments':json.dumps({'target':'inspect_lantern_base','reason':'想调查'})}}]}}]}
        with patch.object(app,'model_id',return_value='test'),patch.object(app,'lm',side_effect=[call,RuntimeError('失败')]):
            with self.assertRaises(RuntimeError): app.chat('你好',sid)
        with app.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM intentions WHERE session_id=? AND status='active'",(sid,)).fetchone()[0],0)

    def test_background_can_form_intention_without_performing_action(self):
        with app.connect() as c:
            sid=app.create_session(c)
            c.execute("UPDATE story_world SET voices_heard=1,clock_started_at=1000,last_contact_at=1000 WHERE session_id=?",(sid,))
        decision={'choices':[{'finish_reason':'tool_calls','message':{'tool_calls':[{'function':{'name':'set_intention','arguments':json.dumps({'target':'read_note','reason':'等准备好后再看'})}}]}}]}
        with patch.object(app,'model_id',return_value='test'),patch.object(app,'lm',return_value=decision):
            app.background_story_tick(4600)
        with app.connect() as c:
            self.assertEqual(c.execute('SELECT note_state FROM story_world WHERE session_id=?',(sid,)).fetchone()[0],'unopened')
            self.assertEqual(c.execute('SELECT status FROM intentions WHERE session_id=?',(sid,)).fetchone()[0],'active')

    def test_legacy_migration_preserves_data(self):
        import sqlite3
        legacy = Path(self.temp.name) / 'legacy.db'
        with sqlite3.connect(legacy) as c:
            c.executescript("""CREATE TABLE messages (id INTEGER PRIMARY KEY, role TEXT NOT NULL, content TEXT NOT NULL);
            CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT NOT NULL UNIQUE);
            CREATE TABLE world (id INTEGER PRIMARY KEY CHECK(id=1), rooms INTEGER NOT NULL, coins INTEGER NOT NULL);
            CREATE TABLE quotes (id INTEGER PRIMARY KEY, days INTEGER NOT NULL, price INTEGER NOT NULL, status TEXT NOT NULL);
            CREATE TABLE events (id INTEGER PRIMARY KEY, content TEXT NOT NULL);
            INSERT INTO messages VALUES (1, 'user', '旧对话');
            INSERT INTO memories VALUES (1, '旧记忆');
            INSERT INTO world VALUES (1, 1, 43);
            INSERT INTO quotes VALUES (1, 3, 57, 'accepted');
            INSERT INTO events VALUES (1, '旧事件');""")
        with patch.object(app, 'DB', legacy):
            app.init()
            app.init()
            with app.connect() as c:
                self.assertEqual(app.state(c)['world'], {'rooms':1,'coins':43})
                for table in ('messages','memories','quotes','events'):
                    self.assertEqual(c.execute(f'SELECT session_id FROM {table}').fetchone()[0], 1)
                self.assertEqual(c.execute('SELECT kind FROM memories').fetchone()[0], 'legacy')

    def test_stream_tool_fragments_reasoning_and_history(self):
        rounds = [
            [{'reasoning_content':'先查房'}, {'tool_calls':[{'index':0,'id':'call1','function':{'name':'propose_', 'arguments':'{\"days\":'}}]}, {'tool_calls':[{'index':0,'function':{'name':'room','arguments':'2,\"discount_percent\":0}'}}]}],
            [{'reasoning_content':'报价已生成'}, {'content':'两晚'}, {'content':'共 40 金币。'}],
        ]
        class Model(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                assert body['stream'] is True
                self.send_response(200); self.end_headers()
                for delta in rounds.pop(0):
                    chunk = {'choices':[{'delta':delta}]}
                    self.wfile.write(('data: '+json.dumps(chunk,ensure_ascii=False)+'\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
        model = ThreadingHTTPServer(('127.0.0.1',0), Model)
        threading.Thread(target=model.serve_forever,daemon=True).start()
        events = []
        try:
            with patch.object(app, 'LM_URL', f'http://127.0.0.1:{model.server_port}/v1'), patch.object(app, 'model_id', return_value='test'):
                result = app.chat('住两晚', 1, lambda kind, data: events.append((kind,data)))
            self.assertEqual(result['reply'], '两晚共 40 金币。')
            self.assertEqual(result['quote']['price'], 40)
            self.assertIn(('tool',''), events)
            self.assertEqual(''.join(v for k,v in events if k=='reasoning'), '先查房报价已生成')
            with app.connect() as c:
                self.assertEqual(c.execute("SELECT reasoning FROM messages WHERE role='assistant'").fetchone()[0], '先查房报价已生成')
        finally:
            model.shutdown(); model.server_close()

    def test_stream_failure_rolls_back_tools(self):
        def fake_stream(payload, emit):
            if len(payload['messages']) == 2:
                return {'role':'assistant','content':'','tool_calls':[{'id':'1','function':{'name':'remember','arguments':'{\"memory\":\"新记忆\"}'}}]}
            raise RuntimeError('流中断')
        with patch.object(app, 'model_id', return_value='test'), patch.object(app, 'lm_stream', side_effect=fake_stream):
            with self.assertRaises(RuntimeError):
                app.chat('你好', 1, lambda kind, data: None)
        with app.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM memories').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT count(*) FROM messages').fetchone()[0], 0)

    def test_length_limit_forwards_last_content_without_saving(self):
        class Model(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200); self.end_headers()
                chunk = {'choices':[{'delta':{'content':'未完成的回复'},'finish_reason':'length'}]}
                self.wfile.write(('data: '+json.dumps(chunk,ensure_ascii=False)+'\n\n').encode())
        model = ThreadingHTTPServer(('127.0.0.1',0), Model)
        threading.Thread(target=model.serve_forever,daemon=True).start()
        events=[]
        try:
            with patch.object(app, 'LM_URL', f'http://127.0.0.1:{model.server_port}/v1'), patch.object(app, 'model_id', return_value='test'):
                with self.assertRaisesRegex(RuntimeError, '未完成且未保存'):
                    app.chat('你好', 1, lambda kind, data: events.append((kind,data)))
            self.assertEqual(events, [('content','未完成的回复')])
            with app.connect() as c:
                self.assertEqual(c.execute('SELECT count(*) FROM messages').fetchone()[0], 0)
        finally:
            model.shutdown(); model.server_close()

    def test_http_stream_emits_events_and_persists_reasoning(self):
        def fake_stream(payload, emit):
            emit('reasoning','思考中')
            emit('content','欢迎')
            return {'role':'assistant','content':'欢迎'}
        server = ThreadingHTTPServer(('127.0.0.1',0), app.Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/chat/stream',
                data=json.dumps({'session_id':1,'message':'你好'}).encode(),
                headers={'Host':'127.0.0.1:8765','Origin':'http://127.0.0.1:8765','Content-Type':'application/json'})
            with patch.object(app, 'model_id', return_value='test'), patch.object(app, 'lm_stream', side_effect=fake_stream):
                with urllib.request.urlopen(req) as response:
                    events = [json.loads(line) for line in response]
            self.assertEqual([event['type'] for event in events], ['reasoning','content','done'])
            self.assertEqual(events[-1]['data']['reply'], '欢迎')
            with app.connect() as c:
                self.assertEqual(c.execute("SELECT reasoning FROM messages WHERE role='assistant'").fetchone()[0], '思考中')
        finally:
            server.shutdown(); server.server_close()

    def test_openai_responses_stream_tools_and_stateless_continuation(self):
        requests=[]
        class OpenAI(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path,self.headers.get('Authorization'),body))
                self.send_response(200);self.end_headers()
                if len(requests)==1:
                    output=[{'type':'reasoning','id':'rs_1','encrypted_content':'opaque','summary':[]}, {'type':'function_call','call_id':'call_1','name':'inspect_surroundings','arguments':'{\"target\":\"lantern_base\"}'}]
                    chunks=[{'type':'response.reasoning_summary_text.delta','delta':'先观察。'}]
                else:
                    output=[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'我看见了。'}]}]
                    chunks=[{'type':'response.output_text.delta','delta':'我看'},{'type':'response.output_text.delta','delta':'见了。'}]
                chunks.append({'type':'response.completed','response':{'status':'completed','output':output}})
                for event in chunks:
                    self.wfile.write(('data: '+json.dumps(event,ensure_ascii=False)+'\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
        server=ThreadingHTTPServer(('127.0.0.1',0),OpenAI)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            with app.connect() as c:
                sid=app.create_session(c)
                app.execute_tool(c,'remember',{'memory':'其他会话的私有信息'},1)
            seen=[]
            with patch.object(app,'OPENAI_URL',f'http://127.0.0.1:{server.server_port}/v1'),patch.object(app,'OPENAI_KEY','test-key'),patch.object(app,'model_id',side_effect=AssertionError('不应调用本地模型')):
                result=app.chat('请看看旧灯',sid,lambda k,v:seen.append((k,v)),provider='openai')
            self.assertEqual(result['reply'],'我看见了。')
            self.assertIn(('reasoning','先观察。'),seen)
            self.assertEqual(requests[0][0],'/v1/responses')
            self.assertEqual(requests[0][1],'Bearer test-key')
            first=requests[0][2];second=requests[1][2]
            self.assertNotIn('其他会话的私有信息',str(first['input']))
            self.assertEqual(first['model'],'gpt-6-luna')
            self.assertFalse(first['store'])
            self.assertTrue(first['stream'])
            self.assertTrue(all(t['type']=='function' and 'name' in t for t in first['tools']))
            self.assertEqual(second['input'][-1]['type'],'function_call_output')
            self.assertEqual(second['input'][-1]['call_id'],'call_1')
            self.assertTrue(any(i.get('encrypted_content')=='opaque' for i in second['input']))
            with app.connect() as c:
                self.assertEqual(c.execute('SELECT lantern_mark_seen FROM story_world WHERE session_id=?',(sid,)).fetchone()[0],1)
                self.assertEqual(c.execute("SELECT reasoning FROM messages WHERE session_id=? AND role='assistant'",(sid,)).fetchone()[0],'先观察。')
        finally:
            server.shutdown();server.server_close()

    def test_openai_incomplete_rolls_back_and_invalid_provider_rejected(self):
        class OpenAI(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200);self.end_headers()
                self.wfile.write(b'data: {"type":"response.incomplete"}\n\n')
        server=ThreadingHTTPServer(('127.0.0.1',0),OpenAI)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            with app.connect() as c: sid=app.create_session(c)
            with patch.object(app,'OPENAI_URL',f'http://127.0.0.1:{server.server_port}/v1'),patch.object(app,'OPENAI_KEY','test-key'):
                with self.assertRaises(RuntimeError): app.chat('你好',sid,lambda k,v:None,provider='openai')
                with self.assertRaises(ValueError): app.chat('你好',sid,provider='other')
            with app.connect() as c:
                self.assertEqual(c.execute('SELECT count(*) FROM messages WHERE session_id=?',(sid,)).fetchone()[0],0)
                self.assertEqual(c.execute('SELECT voices_heard FROM story_world WHERE session_id=?',(sid,)).fetchone()[0],0)
        finally:
            server.shutdown();server.server_close()

    def test_http_agent_booking_and_restart_state(self):
        requests = []
        class Model(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                self.respond({'data':[{'id':'qwen3.8-27b-mlx'}]})
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(body)
                if len(requests) == 1:
                    message = {'role':'assistant','tool_calls':[{'id':'quote1','type':'function','function':{'name':'propose_room','arguments':'{"days":3,"discount_percent":10}'}}]}
                else:
                    message = {'role':'assistant','content':'三晚共 54 金币，请确认。'}
                self.respond({'choices':[{'message':message}]})
            def respond(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
        server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        for instance in (model, server):
            threading.Thread(target=instance.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        def request(path, body=None):
            data = json.dumps(body).encode() if body is not None else None
            headers = {'Host':'127.0.0.1:8765', 'Origin':'http://127.0.0.1:8765', 'Content-Type':'application/json'}
            with urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=headers)) as response:
                return json.load(response)
        try:
            with patch.object(app, 'LM_URL', f'http://127.0.0.1:{model.server_port}/v1'), patch.object(app, 'MODEL', ''), patch.object(app, 'TOKEN', ''):
                reply = request('/api/chat', {'message':'我想住三晚','session_id':1})
                self.assertEqual(reply['world'], {'rooms':2, 'coins':100})
                self.assertEqual(reply['quote']['price'], 54)
                self.assertEqual(requests[0]['model'], 'qwen3.8-27b-mlx')
                self.assertEqual(requests[1]['messages'][-1]['role'], 'tool')
                result = request('/api/confirm', {'quote_id':reply['quote']['id'],'session_id':1})
                self.assertEqual(result['world'], {'rooms':1, 'coins':46})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request('/api/confirm', {'quote_id':reply['quote']['id'],'session_id':1})
                self.assertEqual(error.exception.code, 400)
                app.init()
                state = request('/api/state')
                self.assertEqual(state['world'], {'rooms':1, 'coins':46})
                self.assertEqual(len(state['messages']), 2)
        finally:
            for instance in (server, model):
                instance.shutdown()
                instance.server_close()

if __name__ == '__main__': unittest.main()
