import time
import json
import os
import shutil
import unittest
from unittest.mock import patch

import neko


def chat_message(message_id, author, content='', **extra):
    message = {
        'id': message_id,
        'author': {'username': author},
        'content': content,
    }
    message.update(extra)
    return message


class ChatTriggerTests(unittest.TestCase):
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
    def test_intention_is_judged_before_chat_context_and_memory(self):
        state = neko.default_state()
        state['last_chat_id'] = 100
        message = chat_message(101, 'alice', '今天天气不错')
        events = []

        with patch.object(neko, 'fetch_lobby_context', return_value=[]), \
                patch.object(neko, 'fetch_lobby_new', return_value=[message]), \
                patch.object(neko, 'archive_public_message'), \
                patch.object(neko, 'chat_trigger', return_value=(100, 40, False, False, '随缘聊天')), \
                patch.object(neko, 'get_chat_intention',
                             side_effect=lambda *args: events.append(('intention', args)) or 100), \
                patch.object(neko, 'build_chat_context',
                             side_effect=lambda *_args: events.append(('context',)) or ''), \
                patch.object(neko, 'vector_memory_context',
                             side_effect=lambda *_args, **_kwargs: events.append(('memory',)) or ''), \
                patch.object(neko, 'build_chat_reply', return_value='收到。'), \
                patch.object(neko, 'claim_trigger'), \
                patch.object(neko, 'send_chat_message', return_value=False), \
                patch.object(neko, 'save_state'):
            with patch.object(neko, 'roll_intention',
                              side_effect=lambda _p, _i, _label, fn: fn(40) >= 0 or True):
                neko.poll_chat(state)

        self.assertEqual('intention', events[0][0])
        self.assertEqual(['intention', 'context', 'memory'], [event[0] for event in events])
        self.assertEqual(3, len(events[0][1]))

    def test_no_new_chat_prints_no_new_content(self):
        state = neko.default_state()
        state['last_chat_id'] = 100

        with patch.object(neko, 'fetch_lobby_context', return_value=[]) as context, \
                patch.object(neko, 'fetch_lobby_new', return_value=[]), \
                patch('builtins.print') as output:
            neko.poll_chat(state)

        context.assert_not_called()
        output.assert_any_call('没有新内容。')

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
                patch.object(neko, 'handle_chat_message', side_effect=lambda _s, m, _t: handled.append(m['id'])), \
                patch.object(neko, 'save_state'):
            neko.poll_chat(state)

        self.assertEqual([102, 101], handled)
        self.assertEqual(102, state['last_chat_id'])

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

    def test_semantic_similarity_stays_stronger_than_small_recent_bonus(self):
        path = neko._public_memory_path()
        neko._store_memory(path, 'old', 'lobby', '我今天想学习微积分和导数',
                           created_at=time.time() - 7 * 86400)
        neko._store_memory(path, 'recent', 'lobby', '晚饭吃了一碗面',
                           created_at=time.time())

        rows = neko._search_memory(path, '微积分导数怎么学')

        self.assertEqual('我今天想学习微积分和导数', rows[0][5])

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
