import time
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
                patch.object(neko, 'memory_context', return_value=''), \
                patch.object(neko, 'claim_trigger', side_effect=lambda *_: events.append('claim')) as claim, \
                patch.object(neko, 'send_chat_message', side_effect=lambda *_a, **_k: events.append('send') or True) as send, \
                patch.object(neko, 'remember'), patch.object(neko, 'note_reply'):
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
    def test_direct_message_is_processed_first_and_cursor_does_not_jump_to_context(self):
        state = neko.default_state()
        state['last_chat_id'] = 100
        ambient = chat_message(101, 'bob', '今天天气不错')
        direct = chat_message(102, 'alice', '@neko 在吗')
        newer_context = chat_message(900, 'carol', '上下文页已经更靠后')
        handled = []

        with patch.object(neko, 'fetch_lobby_context', return_value=[newer_context]), \
                patch.object(neko, 'fetch_lobby_new', return_value=[ambient, direct]), \
                patch.object(neko, 'handle_chat_message', side_effect=lambda _s, m, _t: handled.append(m['id'])), \
                patch.object(neko, 'save_state'):
            neko.poll_chat(state)

        self.assertEqual([102, 101], handled)
        self.assertEqual(102, state['last_chat_id'])


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


if __name__ == '__main__':
    unittest.main()
