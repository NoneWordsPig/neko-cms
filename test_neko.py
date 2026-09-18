import time
import json
import os
import shutil
import unittest
from unittest.mock import patch

import neko
from neko_bot.memory import ebbinghaus_retention


def chat_message(message_id, author, content='', **extra):
    message = {
        'id': message_id,
        'author': {'username': author},
        'content': content,
    }
    message.update(extra)
    return message


class ChatTriggerTests(unittest.TestCase):
    def test_plain_name_in_normal_sentence_is_direct_and_addressed(self):
        for content in ('今天neko怎么样', '今天Neko怎么样', '今天NEKO怎么样', '今天妮可酱怎么样'):
            with self.subTest(content=content):
                trigger = neko.chat_trigger(chat_message(0, 'alice', content), None)
                self.assertEqual((100, 100, True, True), trigger[:4])

    def test_both_public_names_are_direct_and_addressed(self):
        for content in ('@neko 能看看这个吗', '@妮可 能看看这个吗'):
            with self.subTest(content=content):
                trigger = neko.chat_trigger(chat_message(1, 'alice', content), None)
                self.assertEqual((100, 100, True, True), trigger[:4])

    def test_reply_to_neko_is_case_insensitive(self):
        message = chat_message(
            2,
            'alice',
            '接着说',
            reply={'author_name': neko.USERNAME.upper(), 'content': '上一句'},
        )
        trigger = neko.chat_trigger(message, None)
        self.assertEqual((100, 85, True, True), trigger[:4])

    def test_addressed_message_bypasses_ambient_cooldown(self):
        state = neko.default_state()
        state['last_chat_reply_at'] = time.time()
        message = chat_message(3, 'alice', '@neko 现在能回我吗')
        events = []

        with patch.object(neko, 'collect_message_images', return_value=[]), \
                patch.object(neko, 'build_chat_reply', return_value='@alice 能，刚看到。'), \
                patch.object(neko, 'vector_memory_context', return_value=''), \
                patch.object(neko, 'claim_trigger', side_effect=lambda *_: events.append('claim')) as claim, \
                patch.object(neko, 'send_chat_message', side_effect=lambda *_a, **_k: events.append('send') or True) as send, \
                patch.object(neko, 'note_reply'):
            neko.handle_chat_message(state, message, [message])

        claim.assert_called_once_with(state, 'chat', 3)
        send.assert_called_once_with('@alice 能，刚看到。', reply_to=3)
        self.assertEqual(['claim', 'send'], events)

    def test_name_mention_skips_random_gate_and_name_only_message_is_replyable(self):
        state = neko.default_state()
        message = chat_message(30, 'alice', '@妮可')

        with patch.object(neko, 'collect_message_images', return_value=[]), \
                patch.object(neko, 'build_chat_reply', return_value='@alice 收到啦。') as build, \
                patch.object(neko, 'vector_memory_context', return_value=''), \
                patch.object(neko, 'roll_intention', side_effect=AssertionError('name mention must not roll')), \
                patch.object(neko, 'claim_trigger') as claim, \
                patch.object(neko, 'send_chat_message', return_value=True) as send, \
                patch.object(neko, 'note_reply'):
            neko.handle_chat_message(state, message, [message])

        build.assert_called_once()
        self.assertEqual('@妮可', build.call_args.args[1])
        claim.assert_called_once_with(state, 'chat', 30)
        send.assert_called_once_with('@alice 收到啦。', reply_to=30)

    def test_adjacent_identical_name_messages_each_trigger_once(self):
        state = neko.default_state()
        state['last_chat_reply_at'] = time.time()
        first = chat_message(31, 'alice', '今天neko怎么样')
        second = chat_message(32, 'alice', '今天neko怎么样')
        events = []

        def claim(state_arg, kind, message_id):
            events.append(('claim', message_id))
            state_arg['handled_chat'].append(message_id)

        with patch.object(neko, 'collect_message_images', return_value=[]), \
                patch.object(neko, 'build_chat_reply', side_effect=['@alice 第一条收到啦。', '@alice 第二条也收到啦。']), \
                patch.object(neko, 'vector_memory_context', return_value=''), \
                patch.object(neko, 'roll_intention', side_effect=AssertionError('name mention must not roll')), \
                patch.object(neko, 'claim_trigger', side_effect=claim), \
                patch.object(neko, 'send_chat_message', side_effect=lambda text, **kwargs: events.append(('send', kwargs['reply_to'])) or True), \
                patch.object(neko, 'note_reply'):
            neko.handle_chat_message(state, first, [first])
            neko.handle_chat_message(state, second, [first, second])

        self.assertEqual([
            ('claim', 31), ('send', 31),
            ('claim', 32), ('send', 32),
        ], events)

    def test_handled_direct_message_is_deduplicated_before_image_or_send(self):
        state = neko.default_state()
        state['handled_chat'].append(4)
        message = chat_message(4, 'alice', '@neko 别重复回')

        with patch.object(neko, 'collect_message_images') as images, \
                patch.object(neko, 'send_chat_message') as send:
            neko.handle_chat_message(state, message, [message])

        images.assert_not_called()
        send.assert_not_called()

    def test_current_account_is_always_in_bot_filter(self):
        self.assertTrue(neko.is_bot(neko.USERNAME))

    def test_bot_message_is_visible_but_never_triggers_reply(self):
        state = neko.default_state()
        message = chat_message(5, 'Logos', '@neko 这条只能看，不能回')

        with patch.object(neko, 'collect_message_images') as images, \
                patch.object(neko, 'send_chat_message') as send:
            neko.handle_chat_message(state, message, [message])

        context = neko.build_chat_context([message, chat_message(6, 'alice', '@neko 你怎么看')], 6)
        self.assertIn('Logos（其他 AI bot）: @neko 这条只能看，不能回', context)
        images.assert_not_called()
        send.assert_not_called()

    def test_own_and_other_bot_messages_are_labeled_in_context(self):
        target = chat_message(13, 'alice', '@neko 评价一下 Logos 的说法')
        timeline = [
            chat_message(10, neko.USERNAME, '我刚才已经回答过这个问题'),
            chat_message(11, 'Logos', '我认为应该换一种做法'),
            chat_message(12, 'bob', '你们两个说得不一样'),
            target,
        ]

        context = neko.build_chat_context(timeline, target['id'])

        self.assertIn(f'{neko.USERNAME}（你自己）: 我刚才已经回答过这个问题', context)
        self.assertIn('Logos（其他 AI bot）: 我认为应该换一种做法', context)
        self.assertIn('bob: 你们两个说得不一样', context)


class ChatPollingTests(unittest.TestCase):
    def test_direct_message_is_processed_first_and_cursor_does_not_jump_to_context(self):
        state = neko.default_state()
        state['last_chat_id'] = 100
        ambient = chat_message(101, 'bob', '今天天气不错')
        direct = chat_message(102, 'alice', '@neko 在吗')
        newer_context = chat_message(900, 'carol', '上下文页已经更靠后')
        handled = []

        with patch.object(neko, 'fetch_lobby_context', return_value=[newer_context]), \
                patch.object(neko, 'fetch_lobby_new', return_value=[ambient, direct]), \
                patch.object(neko, 'archive_public_message'), \
                patch.object(neko, 'handle_chat_message', side_effect=lambda _s, m, _t: (handled.append(m['id']), _s['handled_chat'].append(m['id']))), \
                patch.object(neko, 'save_state'):
            neko.poll_chat(state)

        self.assertEqual([102, 101], handled)
        self.assertEqual(102, state['last_chat_id'])

    def test_unclaimed_name_mention_is_not_lost_to_cursor(self):
        state = neko.default_state()
        state['last_chat_id'] = 100
        ambient = chat_message(101, 'bob', '今天天气不错')
        direct = chat_message(102, 'alice', '@neko 在吗')

        with patch.object(neko, 'fetch_lobby_context', return_value=[]), \
                patch.object(neko, 'fetch_lobby_new', return_value=[ambient, direct]), \
                patch.object(neko, 'archive_public_message'), \
                patch.object(neko, 'handle_chat_message'), \
                patch.object(neko, 'save_state'):
            neko.poll_chat(state)

        self.assertEqual(101, state['last_chat_id'])

    def test_private_history_walks_backwards_until_the_first_message(self):
        latest = [chat_message(i, 'alice', str(i)) for i in range(101, 201)]
        older = [chat_message(i, 'alice', str(i)) for i in range(1, 101)]

        with patch.object(neko, 'fetch_channel_latest', return_value=latest), \
                patch.object(neko, 'get_json', side_effect=[{'messages': older}, {'messages': []}]):
            history = neko.fetch_channel_history('dm-alice')

        self.assertEqual(list(range(1, 201)), [message['id'] for message in history])


class ReplyFormattingTests(unittest.TestCase):
    def test_lobby_direct_reply_gets_exactly_one_mention(self):
        with patch.object(neko, 'get_llm_response', return_value='@Alice：先看报错的第一行。'):
            reply = neko.build_chat_reply('Alice', '代码报错了', 'Alice: 代码报错了', True)
        self.assertEqual('@Alice 先看报错的第一行。', reply)

    def test_reply_reference_keeps_quoted_content(self):
        message = chat_message(
            4,
            'alice',
            '这个呢',
            reply={'author_name': 'bob', 'content': '前一个具体问题'},
        )
        self.assertEqual('这条消息引用了 bob：前一个具体问题', neko.build_reply_reference(message))


class VectorMemoryTests(unittest.TestCase):
    def setUp(self):
        self.old_memory_db_dir = neko.MEMORY_DB_DIR
        self.old_memory_dir = neko.MEMORY_DIR
        self.test_memory_db_dir = os.path.join(neko.SCRIPT_DIR, '_test_memory_db')
        self.test_memory_json_dir = os.path.join(neko.SCRIPT_DIR, '_test_memory_json')
        shutil.rmtree(self.test_memory_db_dir, ignore_errors=True)
        shutil.rmtree(self.test_memory_json_dir, ignore_errors=True)
        os.makedirs(self.test_memory_db_dir, exist_ok=True)
        os.makedirs(self.test_memory_json_dir, exist_ok=True)
        neko.MEMORY_DB_DIR = self.test_memory_db_dir
        neko.MEMORY_DIR = self.test_memory_json_dir

    def tearDown(self):
        neko.MEMORY_DB_DIR = self.old_memory_db_dir
        neko.MEMORY_DIR = self.old_memory_dir
        shutil.rmtree(self.test_memory_db_dir, ignore_errors=True)
        shutil.rmtree(self.test_memory_json_dir, ignore_errors=True)

    def test_private_databases_are_separated_per_person_and_keep_full_text(self):
        long_text = '猫' * 1000
        neko._store_memory(neko._private_memory_path('Alice'), 'chat:1', 'dm', long_text,
                           actor_name='Alice', owner_name='Alice')

        alice_rows = neko._search_memory(neko._private_memory_path('Alice'), '猫')
        bob_rows = neko._search_memory(neko._private_memory_path('Bob'), '猫')

        self.assertEqual(long_text, alice_rows[0][5])
        self.assertEqual([], bob_rows)
        self.assertNotEqual(neko._private_memory_path('Alice'), neko._private_memory_path('Bob'))

    def test_semantic_similarity_stays_stronger_than_recency_decay(self):
        path = neko._public_memory_path()
        neko._store_memory(path, 'old', 'lobby', '我今天想学习微积分和导数',
                           created_at=time.time() - 7 * 86400)
        neko._store_memory(path, 'recent', 'lobby', '晚饭吃了一碗面',
                           created_at=time.time())

        rows = neko._search_memory(path, '微积分导数怎么学')

        self.assertEqual('我今天想学习微积分和导数', rows[0][5])

    def test_ebbinghaus_retention_is_exponential_and_monotonic(self):
        stability = 30 * 86400

        self.assertAlmostEqual(1.0, ebbinghaus_retention(0, stability))
        self.assertAlmostEqual(1 / 2.718281828459045,
                               ebbinghaus_retention(stability, stability), places=7)
        self.assertGreater(
            ebbinghaus_retention(7 * 86400, stability),
            ebbinghaus_retention(30 * 86400, stability),
        )

    def test_equal_vector_matches_are_ranked_by_forgetting_curve(self):
        path = neko._public_memory_path()
        now = time.time()
        text = '周末一起去看星星和月亮'
        neko._store_memory(path, 'old-same-topic', 'lobby', text,
                           created_at=now - neko.MEMORY_STABILITY)
        neko._store_memory(path, 'new-same-topic', 'lobby', text,
                           created_at=now)

        rows = neko._search_memory(path, text)

        self.assertEqual([text, text], [row[5] for row in rows[:2]])
        self.assertGreater(rows[0][0], rows[1][0])
        self.assertAlmostEqual(2.718281828459045, rows[0][0] / rows[1][0], delta=0.02)

    def test_blog_archive_primes_without_backfill_then_stores_new_blog(self):
        state = neko.default_state()
        old_blog = {'id': 'old', 'title': '旧文', 'description': '旧引言', 'author': 'alice'}
        new_blog = {'id': 'new', 'title': '新文', 'description': '新引言', 'author': 'bob'}

        with patch.object(neko, 'fetch_recent_blogs', return_value=[old_blog]), \
                patch.object(neko, 'save_state'):
            neko.poll_public_blogs(state)
        self.assertFalse(neko._search_memory(neko._public_memory_path(), '旧文'))

        with patch.object(neko, 'fetch_recent_blogs', return_value=[new_blog, old_blog]), \
                patch.object(neko, 'save_state'):
            neko.poll_public_blogs(state)
        rows = neko._search_memory(neko._public_memory_path(), '新文新引言')
        self.assertEqual('博客《新文》，作者 bob：新引言', rows[0][5])

    def test_legacy_migration_imports_only_private_history(self):
        entries = [
            {'t': time.time() - 100, 'kind': 'dm', 'where': '私聊', 'who': 'Alice',
             'said': '我喜欢星星', 'me': '我记住啦'},
            {'t': time.time() - 50, 'kind': 'chat', 'where': '大区', 'who': 'Bob',
             'said': '这是旧大区内容', 'me': '旧回复'},
        ]
        archive = os.path.join(self.test_memory_json_dir, '2026-09-16.jsonl')
        with open(archive, 'w', encoding='utf-8') as file:
            for entry in entries:
                file.write(json.dumps(entry, ensure_ascii=False) + '\n')

        neko.migrate_legacy_private_memory()

        private_rows = neko._search_memory(neko._private_memory_path('Alice'), '星星')
        self.assertIn('Alice: 我喜欢星星', private_rows[0][5])
        self.assertFalse(os.path.exists(neko._public_memory_path()))


if __name__ == '__main__':
    unittest.main()
