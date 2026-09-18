"""Domain-level access to comments, blogs and chat channels."""


class SiteRepository:
    def __init__(self, get_json, post_json=None, response_ok=None):
        self.get_json = get_json
        self.post_json = post_json
        self.response_ok = response_ok

    def recent_comments(self):
        return self.get_json('/api/spider/comments')

    def recent_blogs(self):
        data = self.get_json('/api/blogs?page=1&per_page=50&sort=created')
        return (data or {}).get('blogs') or []

    def comment(self, comment_id):
        return self.get_json(f'/api/spider/comments/{comment_id}')

    def blog(self, blog_id):
        return self.get_json(f'/api/spider/blogs/{blog_id}')

    def comment_tree(self, blog_id):
        return (self.get_json(f'/api/blogs/{blog_id}/comments') or {}).get('comments') or []

    def channel_latest(self, channel_id, limit):
        data = self.get_json(f'/api/chat/channels/{channel_id}/messages?limit={limit}')
        return (data or {}).get('messages') or []

    def channel_new(self, channel_id, after_id, limit, max_pages=5):
        messages = []
        after = after_id
        for _ in range(max_pages):
            data = self.get_json(
                f'/api/chat/channels/{channel_id}/messages?after={after}&limit={limit}'
            )
            page = (data or {}).get('messages') or []
            if not page:
                break
            messages.extend(page)
            after = page[-1]['id']
            if len(page) < limit:
                break
        return messages

    def channels(self):
        return (self.get_json('/api/chat/poll') or {}).get('channels') or []

    @staticmethod
    def find_comment_path(nodes, target_id, depth=0):
        if depth > 50:
            return []
        for node in nodes or []:
            if node.get('id') == target_id:
                return [node]
            hit = SiteRepository.find_comment_path(
                node.get('children') or [], target_id, depth + 1
            )
            if hit:
                return [node] + hit
        return []

    @staticmethod
    def merge_timeline(*message_lists):
        seen = set()
        merged = []
        for messages in message_lists:
            for message in messages or []:
                if message.get('id') not in seen:
                    seen.add(message.get('id'))
                    merged.append(message)
        return sorted(merged, key=lambda message: message['id'])
