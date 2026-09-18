"""Polling use-cases.

The service receives a dependency namespace.  The executable facade supplies
its functions, keeping IO replaceable without coupling this module to globals.
"""


class PollingService:
    def __init__(self, dependencies):
        self.d = dependencies

    @staticmethod
    def select_new_comments(comments, cursor):
        if not comments or cursor in (None, comments[0]['id']):
            return []
        index = next((i for i, comment in enumerate(comments) if comment['id'] == cursor), -1)
        if index == -1:
            print('上次记住的评论已经掉出最近 100 条了，把整窗当成新的看一眼喵。')
            return list(reversed(comments))
        return list(reversed(comments[:index]))

    def comments(self, state):
        d = self.d
        comments = d.fetch_recent_comments()
        if not comments:
            print('没有新内容。')
            return
        newest = comments[0]['id']
        if state['last_comment_id'] is None:
            if d.REPLAY_BACKLOG:
                print('演习模式：把最近 100 条评论都拿来看一遍喵。')
                for comment in reversed(comments):
                    d.handle_comment(state, comment)
            else:
                print(f'第一次跑：先记住最新评论 {newest}，历史评论不回补喵。')
                print('没有新内容。')
            state['last_comment_id'] = newest
            d.save_state(state)
            return
        batch = self.select_new_comments(comments, state['last_comment_id'])
        if not batch:
            print('没有新内容。')
            return
        for comment in batch:
            d.handle_comment(state, comment)
        state['last_comment_id'] = newest
        d.save_state(state)

    def public_blogs(self, state):
        d = self.d
        try:
            blogs = d.fetch_recent_blogs()
        except Exception as error:
            print('拉新博客失败，这轮不更新公共记忆：', str(error)[:120])
            return
        ids = [blog.get('id') for blog in blogs if blog.get('id')]
        if not state.get('public_blogs_primed'):
            state['public_blogs_primed'] = True
            state['known_public_blog_ids'] = ids
            d.save_state(state)
            print(f'公共记忆库：已从当前 {len(ids)} 篇博客立水印，之后只收录新文。')
            return
        known = set(state.get('known_public_blog_ids') or [])
        new_blogs = [blog for blog in reversed(blogs) if blog.get('id') not in known]
        stored = sum(1 for blog in new_blogs if d.archive_public_blog(blog))
        state['known_public_blog_ids'] = list(dict.fromkeys(ids + list(known)))[:500]
        d.save_state(state)
        if stored:
            print(f'公共记忆库：收录了 {stored} 篇新博客的标题/引言。')

    def direct_messages(self, state):
        d = self.d
        try:
            channels = d.fetch_channel_list()
        except Exception as error:
            print('拉会话列表失败，这轮先跳过私聊喵：', str(error)[:120])
            return
        directs = [channel for channel in channels
                   if channel.get('kind') == 'direct' and channel.get('id')]
        if not state.get('private_memory_primed'):
            all_ok, stored = True, 0
            for channel in directs:
                try:
                    history = d.fetch_channel_history(channel['id'])
                    stored += sum(
                        1 for message in history if d.archive_private_message(message, channel)
                    )
                except Exception as error:
                    all_ok = False
                    print(f"回填私聊[{channel.get('title') or channel['id']}] 失败，下轮继续：",
                          str(error)[:120])
            if all_ok:
                state['private_memory_primed'] = True
                d.save_state(state)
                print(f'私人记忆库：已回填 {len(directs)} 个会话的 {stored} 条历史消息。')
        if not state.get('dm_primed'):
            for channel in directs:
                state['dm_cursors'][channel['id']] = (
                    (channel.get('last_message') or {}).get('id') or 0
                )
            state['dm_primed'] = True
            print(f'第一次跑：先记住 {len(directs)} 个私聊会话的位置，历史私聊不回补喵。')
            if d.REPLAY_BACKLOG:
                print('演习模式：把每个私聊会话最近一页也拿来看一遍喵。')
                for channel in directs:
                    page = d.fetch_channel_latest(channel['id'])
                    for message in page:
                        d.handle_direct_message(state, message, channel, page)
            d.save_state(state)
            return
        for channel in directs:
            channel_id = channel['id']
            cursor = state['dm_cursors'].get(channel_id)
            last_id = ((channel.get('last_message') or {}).get('id')) or 0
            if cursor is None:
                print(f"发现新私聊会话《{channel.get('title')}》，从第一条开始看喵。")
                cursor = 0
            if last_id <= cursor:
                continue
            messages = d.fetch_channel_new(channel_id, cursor)
            if not messages:
                continue
            timeline = d.merge_timeline(d.fetch_channel_latest(channel_id), messages)
            for message in messages:
                d.handle_direct_message(state, message, channel, timeline)
            state['dm_cursors'][channel_id] = max(
                [message['id'] for message in messages] + [cursor]
            )
            d.save_state(state)
            try:
                d.mark_channel_read(channel_id)
            except Exception as error:
                print('标记已读失败：', str(error)[:120])

    def lobby(self, state):
        d = self.d
        if state['last_chat_id'] is None:
            context = d.fetch_lobby_context()
            if context and d.REPLAY_BACKLOG:
                print('演习模式：把聊天区最近一页拿来看一遍喵。')
                for message in context:
                    d.handle_chat_message(state, message, context)
            if context:
                print(f"第一次跑：先记住聊天区最新消息 #{context[-1]['id']}，历史消息不回补喵。")
                state['last_chat_id'] = context[-1]['id']
                d.save_state(state)
                if not d.REPLAY_BACKLOG:
                    print('没有新内容。')
            else:
                print('没有新内容。')
            return
        messages = d.fetch_lobby_new(state['last_chat_id'])
        if not messages:
            print('没有新内容。')
            return
        # after 只按 id 追赶；过期消息不能继续进入处理队列，否则积压时会把旧话
        # 当成新话逐条处理。无论是否过期，都推进已经拉到的最大 id，避免反复拉取。
        recent_messages = [message for message in messages
                           if d.is_recent_chat_message(message)]
        newest_id = max(message['id'] for message in messages)
        if not recent_messages:
            state['last_chat_id'] = newest_id
            d.save_state(state)
            print('最近 30 秒内没有新内容。')
            return
        # 只有发现增量后才读取上下文；无新消息时不再反复读取最近 30 条。
        context = d.fetch_lobby_context()
        for message in recent_messages:
            d.archive_public_message(message)
        timeline = d.merge_timeline(context, recent_messages)
        ordered = sorted(
            enumerate(recent_messages),
            key=lambda pair: (d.chat_message_priority(pair[1], timeline), pair[0]),
        )
        for _, message in ordered:
            d.handle_chat_message(state, message, timeline)
        # 不因某条消息暂未回复而回退游标，否则重启/下一轮会反复处理历史消息。
        state['last_chat_id'] = newest_id
        d.save_state(state)
