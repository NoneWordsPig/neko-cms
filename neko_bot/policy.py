"""Pure reply-trigger policy, independent from networking and persistence."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ReplyPolicy:
    username: str
    bot_usernames: frozenset
    names: tuple
    ambient_probability: int = 25
    ambient_intention: int = 50

    def is_bot(self, name):
        return (name or '').strip().lower() in self.bot_usernames

    @staticmethod
    def same_username(left, right):
        return bool(left and right and left.strip().lower() == right.strip().lower())

    def author_label(self, name):
        if self.same_username(name, self.username):
            return f'{name}（你自己）'
        if self.is_bot(name):
            return f'{name}（其他 AI bot）'
        return name

    def is_mentioned(self, text):
        lowered = (text or '').lower()
        return any(name.lower() in lowered for name in self.names)

    @staticmethod
    def previous_message(timeline, target_id):
        for index, message in enumerate(timeline or []):
            if message.get('id') == target_id:
                return timeline[index - 1] if index > 0 else None
        return None

    def addressing(self, message, previous_message, strip_images=lambda text: text):
        content = strip_images(message.get('content') or '')
        reply = message.get('reply') or {}
        reply_author = reply.get('author_name') or ((reply.get('author') or {}).get('username')) or ''
        previous_author = ((previous_message or {}).get('author') or {}).get('username') or ''
        if self.is_mentioned(content):
            return True, True, '在聊天区叫到了我'
        if self.same_username(reply_author, self.username):
            return True, True, '引用回复了我的消息'
        if self.same_username(previous_author, self.username):
            return False, True, '我刚说完话，对方接着说话'
        return False, False, ''

    def chat_trigger(self, message, previous_message, has_image=False,
                     strip_images=lambda text: text):
        direct, addressed, reason = self.addressing(message, previous_message, strip_images)
        content = strip_images(message.get('content') or '')
        if direct and self.is_mentioned(content):
            return 100, 100, direct, addressed, reason
        if direct:
            return 100, 85, direct, addressed, reason
        if addressed:
            return 100, 70, direct, addressed, reason
        if has_image:
            return (
                self.ambient_probability, self.ambient_intention, False, False,
                '发了张图，感兴趣就搭一句',
            )
        return (
            self.ambient_probability, self.ambient_intention, False, False,
            '随便看看，感兴趣就搭一句',
        )

    def priority(self, message, timeline, strip_images=lambda text: text):
        previous = self.previous_message(timeline, message.get('id'))
        direct, addressed, _ = self.addressing(message, previous, strip_images)
        return 0 if direct else (1 if addressed else 2)
