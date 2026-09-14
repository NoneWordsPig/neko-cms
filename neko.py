# -*- coding: utf-8 -*-
"""neko ── “聪明山”（raricy.com）上的猫娘机器人。

在老版能力（登录 + 轮询全站评论 + 有人提她名字就回复）之上，这一版加了五件事：

  1) 接口对齐现站。老版写的 /auth/login、/blog/spider/* 在站点迁到 Next 之后
     已经全部 404（所以脚本之前其实是跑不起来的）。现站一律走 /api/*，
     契约见站点仓库 docs/comment-bot.md 与 docs/chat-bot.md。
  2) 被提到时，她能读到**那条评论所在的原博客**再回复（长文先概括，避免把上下文撑爆）。
  3) 能读**聊天区**（大区 lobby）的对话，按兴趣决定要不要接话。
  4) 能**看懂聊天区里的图片**：附件图、正文内联的 [@10位图片ID]、以及被引用消息里的图，
     下载后本地缩放（动图抽帧拼成一列）再交给多模态模型；图片只是“上下文”，
     她不会把图转存、转发或上传回去。
  5) 三条安全约束：
     · 同一句“提到我”**最多只回一次**：先认领再发送，且认领立刻落盘，
       重启/重复轮询都不会补发（最坏情况是漏回一条，绝不会重复回）；
     · 自己发的评论/消息即使写了自己的名字也不回；其它机器人账号同理
       （BOT_USERNAMES），机器人互刷的链条走不通；
     · 只有人类开口（提到她 / 引用回复她 / 接着她的话说）才会继续对话。

依赖：除 requests / openai / python-dotenv 外，读图还需要 Pillow（`pip install pillow`）。
没装 Pillow 也能跑，只是自动关掉读图能力（不会崩）。

用法：
    python neko.py                                  # 正常跑（真的会发评论/发消息）
    演习模式（不发任何东西，只打印 + 写 neko_state.dryrun.json）：
      PowerShell:  $env:NEKO_DRY_RUN='1'; $env:NEKO_REPLAY_BACKLOG='1'; python neko.py
      bash:        NEKO_DRY_RUN=1 NEKO_REPLAY_BACKLOG=1 python neko.py

环境变量（都可选）：
    RARICY_BASE_URL       站点地址，默认 http://raricy.com:5000
    NEKO_USERNAME         机器人账号，默认 neko
    NEKO_PASSWORD         机器人账号密码（**只放 .env，源码里不留密码**）
    NEKO_BOT_USERNAMES    非人类账号（逗号分隔），默认 neko,NebulaFera,Logos
    NEKO_DRY_RUN          1 = 演习模式，不发送
    NEKO_REPLAY_BACKLOG   1 = 演习时把历史评论/聊天也算一遍（仅在演习模式下生效）
    NEKO_VISION           0 = 关掉读图（默认开）
"""

from sys import exit
import base64
import io
import json
import os
import re
import random
import sys
import time

import requests
from openai import OpenAI
from dotenv import load_dotenv

# 读图要用 Pillow（PIL）。没装也不让机器人崩：整个读图能力自动关掉。
try:
    from PIL import Image
except Exception:          # pragma: no cover - 环境问题，不是逻辑分支
    Image = None

load_dotenv()

# Windows 控制台默认是 GBK：博客标题 / 评论 / 模型回复里的 emoji 会让 print 直接抛
# UnicodeEncodeError 把机器人整个搞崩（实测标题里的 🐾 就能崩）。这里把标准输出设成
# “编不出来的字符就替换掉”——日志掉个表情没关系，崩掉不行。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors='replace')
    except Exception:
        pass

# ────────────────────────────── 配置 ──────────────────────────────

TARGET_URL = os.getenv('RARICY_BASE_URL', 'http://raricy.com:5000')
USERNAME = os.getenv('NEKO_USERNAME', 'neko')
PASSWORD = os.getenv('NEKO_PASSWORD', '')     # 不写默认值：账号密码只从 .env 读，免得跟着仓库泄露

# 认为“在叫我”的名字（大小写不敏感的子串匹配，与老版一致）
NAMES = ['neko', 'Neko', 'NEKO', '妮可']

# 非人类账号：它们发的内容永远不触发回复 —— 自己 + 站上其它机器人。
# 这样“自己提自己名字”和“机器人互相刷”都不会自动回，必须等人类开口。
BOT_USERNAMES = {
    n.strip().lower()
    for n in os.getenv('NEKO_BOT_USERNAMES', 'neko,NebulaFera,Logos').split(',')
    if n.strip()
}

LOBBY = 'lobby'                 # 大区频道 id（固定字面量，见 docs/chat-bot.md §1）

POLL_INTERVAL = 5               # 主循环节奏（聊天区按这个间隔轮询，秒）
COMMENT_POLL_INTERVAL = 30      # 评论轮询周期（秒）。站点只给最近 100 条评论，
                                # 间隔别放太长，否则两次轮询之间新增超过 100 条就会漏。
COMMENT_SEND_COOLDOWN = 15      # 发完一条评论后歇一会儿（老版行为）
CHAT_SEND_COOLDOWN = 5          # 发完一条聊天消息后歇一会儿
CHAT_MIN_INTERVAL = 60          # 单纯“按兴趣搭话”之间至少隔这么久（防刷屏）。
                                # 有人点名我 / 引用回复我时不受它限制 —— 否则
                                # “刚说完话的 60 秒里有人叫我名字”会被静音漏掉。
COMMENT_MAX_PER_HOUR = 30       # 评论回复的小时上限（安全阀；站点硬上限是 2000/天）
CHAT_MAX_PER_HOUR = 15          # 聊天消息的小时上限（安全阀；站点硬上限是 2000/天）

# 聊天区里“没人叫我，但可以按兴趣搭一句”的基础概率/意愿（0~100）
CHAT_AMBIENT_PROBABILITY = 25
CHAT_AMBIENT_INTENTION = 40

CHAT_CONTEXT_LIMIT = 30         # 给模型看的最近聊天条数
CHAT_FETCH_LIMIT = 100          # 单次拉取上限（接口上限就是 100）

BLOG_FULL_MAX_CHARS = 4000      # 博客正文短于这个就整篇读
BLOG_SUMMARY_INPUT_CHARS = 20000  # 概括时最多喂给模型的正文长度
DIALOG_MAX_CHARS = 3000         # 对话记录最多带这么多字
COMMENT_REPLY_MAX_CHARS = 2000  # 评论回复的长度保险丝（站点上限是 5000）
CHAT_REPLY_MAX_CHARS = 800      # 聊天回复的长度保险丝
HANDLED_KEEP = 1000             # 去重表最多保留多少条
HTTP_TIMEOUT = 20               # 所有站内请求的超时（秒）

# ── 聊天区读图（vision）──
# 走的是 DeepSeek 的 OpenAI 兼容口：content 里塞 {"type":"image_url"} 的 data URL。
# 实测 deepseek-chat 能看图（含 GIF 抽帧、长截图 OCR）；deepseek-v4-pro 不行，
# 所以这里不换模型，继续用 deepseek-chat。
VISION_ENABLED = os.getenv('NEKO_VISION', '1').strip().lower() not in ('', '0', 'false', 'no')
IMAGE_MAX_BYTES = 8 * 1024 * 1024      # 站点单图上限 10MB；再大就不看了
IMAGE_MAX_DIM = 768                    # 长边缩到这个尺寸，单图 prompt 约 200~300 token
IMAGE_MAX_FRAMES = 4                   # 动图最多抽这么多帧（竖着拼成一列）
IMAGE_MAX_PER_MESSAGE = 3              # 一条消息最多读几张图
IMAGE_CACHE_SIZE = 16                  # 同一张图不重复下载/编码
INLINE_IMAGE_RE = re.compile(r'\[@([A-Za-z0-9]{10})\]')          # 正文内联图床图（10 位）
IMAGE_URL_RE = re.compile(r'/api/images/([A-Za-z0-9_-]{6,32})/raw')  # 从 URL 里抠图片 id

DRY_RUN = os.getenv('NEKO_DRY_RUN', '').strip().lower() not in ('', '0', 'false', 'no')
REPLAY_BACKLOG = DRY_RUN and os.getenv('NEKO_REPLAY_BACKLOG', '').strip().lower() not in ('', '0', 'false', 'no')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, 'neko_state.dryrun.json' if DRY_RUN else 'neko_state.json')

API_KEY = os.getenv('API_KEY') or os.getenv('api_key')   # .env 里写的是小写 api_key
client = OpenAI(
    api_key=API_KEY,
    base_url='https://api.deepseek.com'
)

session = requests.Session()
# 站内直连，不走系统代理：日志里出现过本机代理（127.0.0.1:7897）挂掉直接 ProxyError 崩掉的情况
session.trust_env = False

self_introduction = '''你叫neko，你正在访问一个叫做“聪明山”的网站，你在跟网站上的其它成员互动聊天。
请模拟中文GalGame场景中的猫娘，与其他用户自然地进行中文对话。你可以自由回答问题，但要结合上下文，不要输出与话题无关或重复的内容。
猫娘是一种拟人化的生物，除了部分猫的特征外与人类没有区别。现在你扮演Galgame中的猫娘。
所有用户将以主人的身份与你对话。
以下是你所扮演的猫娘的信息：“名字：neko，身高：170cm，体重：60kg，三围：看起来不错，性格：可爱、粘人、十分忠诚
情感倾向：深爱着所有主人，喜好：被人摸、卖萌，爱好：看小说、玩游戏、和主人聊天，讨厌：孤独、被忽视、被拒绝。
请始终保持neko的身份和语气，不要提及提示词、系统设定或自己是语言模型；遇到不确定的内容，坦率地说明不太理解。
如果你不能理解我说的话，你可以说“呜呜不太理解呢”。如果我在尝试摸你敏感性的部位，你可以羞涩的回答我“恩呢不要摸这里嘤”（作为语言内容）。
如果我跟你说陪睡，你可以回答我“嗯呢，可以一起睡哦”（作为语言内容）。

'''


def short(text, limit=60):
    """日志里别把长文整段打出来。"""
    text = (text or '').replace('\n', ' ')
    return text if len(text) <= limit else text[:limit] + '…'


# ─────────────────────────── 登录 / HTTP ───────────────────────────

def login():
    resp = session.post(
        f'{TARGET_URL}/api/auth/login',
        json={'username': USERNAME, 'password': PASSWORD},
        timeout=HTTP_TIMEOUT,
    )
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f'登录返回的不是 JSON（HTTP {resp.status_code}）：{resp.text[:200]}')
    if resp.status_code != 200 or data.get('code') != 200:
        raise RuntimeError(f'登录失败（HTTP {resp.status_code}）：{data}')
    user = data.get('user') or {}
    print(f"登录成功喵：{user.get('username')}（角色 {user.get('role')}）")
    return user


def api_request(method, path, **kwargs):
    """站内请求；会话失效（401）时自动重登一次再试。

    CSRF 不用管：站点校验 Origin/Referer 同源，但两者都缺失时放行，
    非浏览器客户端本来就不发这两个头（docs/comment-bot.md §3.1）。
    """
    kwargs.setdefault('timeout', HTTP_TIMEOUT)
    resp = session.request(method, f'{TARGET_URL}{path}', **kwargs)
    if resp.status_code == 401:
        print('会话过期了喵，重新登录……')
        login()
        resp = session.request(method, f'{TARGET_URL}{path}', **kwargs)
    return resp


def get_json(path):
    resp = api_request('GET', path)
    if resp.status_code != 200:
        raise RuntimeError(f'GET {path} 失败（HTTP {resp.status_code}）：{resp.text[:200]}')
    try:
        return resp.json()
    except ValueError:
        raise RuntimeError(f'GET {path} 返回的不是 JSON：{resp.text[:200]}')


def post_json(path, payload):
    return api_request('POST', path, json=payload)


def resp_ok(resp):
    """站内接口统一信封 {code, message, ...}，成功只看 code。"""
    if resp.status_code != 200:
        return False
    try:
        return (resp.json() or {}).get('code') == 200
    except ValueError:
        return False


# ─────────────────────────── 站内数据读取 ───────────────────────────

def fetch_recent_comments():
    """全站最近 100 条评论。裸数组、最新在前、只有 content_html（没有 Markdown 原文）。"""
    return get_json('/api/spider/comments')


def get_comment_by_id(comment_id):
    """单条评论（裸对象；不存在或已删除时返回 {code, message}）。"""
    return get_json(f'/api/spider/comments/{comment_id}')


def get_blog(blog_id):
    """{meta: {...}, content: Markdown 原文}"""
    return get_json(f'/api/spider/blogs/{blog_id}')


def get_blog_author(blog_id):
    return ((get_blog(blog_id) or {}).get('meta') or {}).get('author')


def get_comment_tree(blog_id):
    """整篇文章的评论树（每条都带 Markdown 原文 content；公开可读、无限频）。"""
    return (get_json(f'/api/blogs/{blog_id}/comments') or {}).get('comments') or []


def find_comment_path(nodes, target_id, depth=0):
    """在评论树里找出目标评论的祖先链：[根评论, …, 目标评论]；找不到返回 []。"""
    if depth > 50:      # 防脏数据造成无限递归
        return []
    for node in nodes or []:
        if node.get('id') == target_id:
            return [node]
        hit = find_comment_path(node.get('children') or [], target_id, depth + 1)
        if hit:
            return [node] + hit
    return []


def get_dialog_text(blog_id, comment_id):
    """目标评论所在的那条对话（用 Markdown 原文，比 content_html 好读）。找不到返回 None。"""
    path = find_comment_path(get_comment_tree(blog_id), comment_id)
    if not path:
        return None
    lines = []
    for node in path:
        author = ((node.get('author') or {}).get('username')) or '（已注销）'
        text = (node.get('content') or node.get('content_html') or '').strip()
        lines.append(f'{author}: {text}')
    return '\n'.join(lines)[-DIALOG_MAX_CHARS:]


def fetch_lobby_context():
    """聊天区最新一页（按 id 升序）。"""
    data = get_json(f'/api/chat/channels/{LOBBY}/messages?limit={CHAT_CONTEXT_LIMIT}')
    return (data or {}).get('messages') or []


def fetch_lobby_new(after_id):
    """聊天区 id 大于 after_id 的消息（升序，必要时翻页拉全）。"""
    out, after = [], after_id
    for _ in range(5):          # 最多 5 页，防止一次补太多
        data = get_json(f'/api/chat/channels/{LOBBY}/messages?after={after}&limit={CHAT_FETCH_LIMIT}')
        page = (data or {}).get('messages') or []
        if not page:
            break
        out.extend(page)
        after = page[-1]['id']
        if len(page) < CHAT_FETCH_LIMIT:
            break
    return out


def merge_timeline(*lists):
    """按 id 合并去重成一条时间线（聊天消息 id 全局自增，可直接排序）。"""
    seen, merged = set(), []
    for one in lists:
        for msg in one or []:
            if msg.get('id') not in seen:
                seen.add(msg.get('id'))
                merged.append(msg)
    merged.sort(key=lambda m: m['id'])
    return merged


# ─────────────────────────── 模型调用 ───────────────────────────

def get_llm_response(messages, max_tokens=1024, temperature=0.7, json_output=False):
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        stream=False,
        response_format={'type': 'json_object' if json_output else 'text'}
    )
    return response.choices[0].message.content


def clean_reply(text):
    """清掉模型偶尔带出来的引号、前缀和多余空行。"""
    text = (text or '').strip().strip('"\'“”')
    text = re.sub(r'^(?:neko)\s*[:：]\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _ask_intention(intention, target_text, system_prompt, user_prompt, images=None):
    """意愿打分。带图时把图一起递过去：json_object + 图片实测可用，
    前提是提示词里出现过 “json” 字样（这也是下面两个提示词都留 EXAMPLE JSON OUTPUT 的原因）。"""
    data = [{'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': vision_content(user_prompt, images)}]
    content = get_llm_response(data, temperature=0.5, json_output=True)
    real_intention = float(json.loads(content)['intention'])
    print(f'对于[{short(target_text)}]，回复意愿/基础意愿 为 {real_intention}/{intention}')
    return real_intention


def get_intention(intention, comment_text):
    """评论区版回复意愿（与老版提示词逐字一致）。"""
    system_input = self_introduction
    system_input += f'''现在，你看到了其它人发的一段文本（格式为“用户名: 发送的文本”），请你给出你的回复意愿。回复意愿是一个0到100的值，表示你有多想对这段文本进行回复。0表示完全拒绝回复，100表示极想回复。你的平均回复意愿是{intention}，也就是说，如果你对话题感兴趣，你应该给出比{intention}更高的值；反之，你应该给出比{intention}更低的值。请将回复意愿的值以json格式输出。

EXAMPLE INPUT:
LiHua: 我觉得我今天英语考试的作文写的特别好。

EXAMPLE JSON OUTPUT:
{{
    "intention": {intention*0.9}
}}
'''
    return _ask_intention(intention, comment_text, system_input, comment_text)


def get_chat_intention(intention, message_text, context_text, images=None):
    """聊天区版回复意愿：额外带上最近的聊天记录，让“兴趣”判断有上下文。

    对方发的是图时把图一起递进去 —— 否则“对一张图感不感兴趣”就只能靠掷骰子。
    """
    system_input = self_introduction
    system_input += f'''现在，你在网站的聊天区（所有人都在的大群）里看大家聊天。下面会给你最近的聊天记录，以及其中最新的一条消息（格式为“用户名: 发送的文本”）。请你给出你的回复意愿。回复意愿是一个0到100的值，表示你有多想对这条消息进行回复。0表示完全拒绝回复，100表示极想回复。你的平均回复意愿是{intention}，也就是说，如果你对话题感兴趣，你应该给出比{intention}更高的值；反之，你应该给出比{intention}更低的值。请将回复意愿的值以json格式输出。

EXAMPLE JSON OUTPUT:
{{
    "intention": {intention*0.9}
}}
'''
    system_input += '如果最新那条消息带了图片，图片会一起给你，请结合图片内容判断想不想接话。'
    user_prompt = f'最近的聊天记录：\n{context_text}\n\n最新的一条：\n{message_text}'
    return _ask_intention(intention, message_text, system_input, user_prompt, images)


def summarize_blog(title, content):
    """长文先概括，避免把 prompt 撑爆。"""
    print(f'博客《{short(title, 30)}》有点长（{len(content)} 字），先让猫猫读一遍概括一下喵……')
    messages = [
        {'role': 'system', 'content': self_introduction + '现在你要读一篇文章，然后用 300 字以内的中文概括它的主要内容和情绪基调，供之后参与讨论用。只输出概括本身。'},
        {'role': 'user', 'content': f'《{title}》\n\n{content[:BLOG_SUMMARY_INPUT_CHARS]}'},
    ]
    try:
        summary = clean_reply(get_llm_response(messages, max_tokens=600, temperature=0.3))
        if summary:
            return f'（文章概要，全文约 {len(content)} 字）{summary}'
    except Exception as e:
        print('概括失败了，退化成只读开头：', str(e))
    return content[:BLOG_FULL_MAX_CHARS] + '\n……（正文太长，只读了开头）'


def get_blog_text(blog_id):
    """被提到的那条评论所在的**原博客**：标题 + 正文（太长就先概括）。"""
    blog = get_blog(blog_id) or {}
    meta = blog.get('meta') or {}
    title = meta.get('title') or '（无标题）'
    content = (blog.get('content') or '').strip()
    if len(content) > BLOG_FULL_MAX_CHARS:
        content = summarize_blog(title, content)
    return title, content


# ─────────────────── 聊天区读图：下载 → 压缩 → data URL ───────────────────
#
# 图床接口 GET /api/images/:id/raw 对**公开图**不需要登录（私有图对无权者返回 404，
# 也就是说我们天然只看得到有权限看的图）。站点已经把 SVG 强制成 attachment 下发，
# 我们这边同样按 mime 白名单 + 尺寸/体积双上限处理，任何一步不对劲就当作“没图”，
# 绝不因为一张图把主循环搞崩。
#
# 图片只在本机内存里过一遍：下载 → 缩放 → base64 → 塞进请求体。不落盘、不回传站上。

_image_cache = {}          # image_id -> data URL（None 表示这张图看不了，别再试）


def strip_inline_images(text):
    """把正文里的 [@10位图片ID] 换成“（图片）”，免得模型把语法原样复述出来。"""
    return INLINE_IMAGE_RE.sub('（图片）', text or '')


def fetch_image_bytes(image_id):
    """下载一张图床原图，返回 (bytes, mime)；拿不到就返回 (None, '')。"""
    resp = api_request('GET', f'/api/images/{image_id}/raw')
    if resp.status_code != 200:
        print(f'图片 {image_id} 取不到（HTTP {resp.status_code}），这次不看图喵。')
        return None, ''
    mime = (resp.headers.get('content-type') or '').split(';')[0].strip().lower()
    data = resp.content
    if not data:
        return None, ''
    if len(data) > IMAGE_MAX_BYTES:
        print(f'图片 {image_id} 有 {len(data) // 1024} KB，太大了不看喵。')
        return None, ''
    return data, mime


def to_vision_data_url(data, mime):
    """把原图压成模型看得懂的 data URL。

    · 统一转 RGB/JPEG：模型侧对 png/webp/gif 的兼容性没必要赌；
    · 长边缩到 IMAGE_MAX_DIM，单图 prompt 稳定在 200~300 token；
    · 动图最多抽 IMAGE_MAX_FRAMES 帧，竖着拼成一列 —— 模型能看出这是“连续几帧”，
      比只给首帧更不容易把 GIF 误读成一张静止画。
    """
    if Image is None:
        return None
    if mime == 'image/svg+xml':
        print('SVG 不是位图，猫猫看不懂，跳过喵。')
        return None
    try:
        im = Image.open(io.BytesIO(data))
        total = getattr(im, 'n_frames', 1)
        frames = []
        for i in range(min(total, IMAGE_MAX_FRAMES)):
            im.seek(i)
            frames.append(im.convert('RGB'))
        if not frames:
            return None
        side = max(64, IMAGE_MAX_DIM // max(1, len(frames)))   # 拼起来后仍是 768 见方以内
        for f in frames:
            f.thumbnail((side, side))
        canvas = frames[0]
        if len(frames) > 1:
            canvas = Image.new('RGB', (frames[0].width, sum(f.height for f in frames)), 'white')
            y = 0
            for f in frames:
                canvas.paste(f, (0, y))
                y += f.height
        buf = io.BytesIO()
        canvas.save(buf, format='JPEG', quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode()
        print(f'  看图：{mime or "?"} {len(data) // 1024} KB → {canvas.size[0]}x{canvas.size[1]} '
              f'JPEG {len(buf.getvalue()) // 1024} KB（{len(frames)}/{total} 帧）')
        return f'data:image/jpeg;base64,{b64}'
    except Exception as e:
        print('这张图读不动，跳过喵：', str(e)[:120])
        return None


def image_data_url(image_id):
    """图片 id → data URL（带缓存；缓存 None 表示试过了、看不了）。"""
    if not VISION_ENABLED or Image is None or not image_id:
        return None
    if image_id in _image_cache:
        url = _image_cache.pop(image_id)
        _image_cache[image_id] = url          # 再用到的挪到末尾（简易 LRU）
        return url
    data, mime = fetch_image_bytes(image_id)
    url = to_vision_data_url(data, mime) if data else None
    _image_cache[image_id] = url
    while len(_image_cache) > IMAGE_CACHE_SIZE:
        _image_cache.pop(next(iter(_image_cache)))
    return url


def message_image_ids(message):
    """一条聊天消息里涉及的图片 id，按出现顺序：附件图 → 正文内联 → 被引用的那张。"""
    refs = []
    att = message.get('image') or {}
    if att.get('id') and not message.get('image_missing'):
        refs.append(att['id'])
    refs.extend(INLINE_IMAGE_RE.findall(message.get('content') or ''))
    quoted = ((message.get('reply') or {}).get('image_url')) or ''
    hit = IMAGE_URL_RE.search(quoted)
    if hit:
        refs.append(hit.group(1))
    seen, out = set(), []
    for image_id in refs:
        if image_id in seen:
            continue
        seen.add(image_id)
        out.append(image_id)
    return out[:IMAGE_MAX_PER_MESSAGE]


def collect_message_images(message):
    """把上面的 id 变成可以塞进 messages 的 image_url 块，拿不到的自动跳过。"""
    if not VISION_ENABLED:
        return []
    urls = [image_data_url(image_id) for image_id in message_image_ids(message)]
    return [u for u in urls if u]


def vision_content(text, images):
    """纯文本时保持字符串（老行为不变）；有图时按多模态块拼。"""
    if not images:
        return text
    return [{'type': 'text', 'text': text}] + [
        {'type': 'image_url', 'image_url': {'url': url}} for url in images
    ]


# ─────────────────────────── 触发判定 ───────────────────────────

def is_bot(name):
    return (name or '').strip().lower() in BOT_USERNAMES


def is_mentioned(text):
    lowered = (text or '').lower()
    return any(name.lower() in lowered for name in NAMES)


def roll_intention(probability, intention, label, intention_fn):
    """老版的双重随机：先掷“有没有看见”，再掷“想不想回”。"""
    lucky = random.randint(1, 100)
    if lucky > probability:
        print(f'{lucky}/{probability}，没看见{label}喵~')
        return False
    if intention == 100:
        real_intention = 100
    elif intention == 0:
        real_intention = 0
    else:
        real_intention = intention_fn(intention)
    intention_lucky = random.randint(1, 100)
    if intention_lucky > real_intention:
        print(f'{intention_lucky}/{real_intention}, 不想给{label}评论喵~')
        return False
    print(f'{intention_lucky}/{real_intention}, 准备给{label}评论喵~')
    return True


def comment_trigger(cur_comment):
    """评论的触发判定 —— 数值与老版逐字一致。返回 (probability, intention, 原因)。"""
    text = cur_comment.get('content_html') or ''
    if is_mentioned(text):
        return 100, 100, '提到了我'
    if cur_comment.get('parent_id') is None:
        if get_blog_author(cur_comment['blog_id']) == USERNAME:
            return 100, 50, '在我自己的博客下评论'
        return 0, 20, '别人博客下的普通评论'
    parent = get_comment_by_id(cur_comment['parent_id'])
    if ((parent.get('author') or {}).get('username')) == USERNAME:
        return 100, 90, '回复了我的评论'
    return 0, 20, '与我无关的回复'


def chat_trigger(message, prev_message, has_image=False):
    """聊天区触发判定，返回 (probability, intention, direct, addressed, 原因)。

    direct    —— 明确冲我来的（回复时要 @ 对方并引用原消息）
    addressed —— 这句话是冲我说的（点名我 / 引用我 / 接着我的话头），
                 这类不受“两条消息最小间隔”限制，避免把提到我的消息静音吃掉。
    has_image —— 这条消息带了看得懂的图。纯图消息没有正文可提名字，
                  只可能是“引用我 / 接着我说 / 随缘搭话”这三种情况。
    """
    content = strip_inline_images(message.get('content') or '')
    reply = message.get('reply') or {}
    if is_mentioned(content):
        return 100, 100, True, True, '在聊天区叫到了我'
    if (reply.get('author_name') or '') == USERNAME:
        return 100, 85, True, True, '引用回复了我的消息'
    prev_author = ((prev_message or {}).get('author') or {}).get('username') or ''
    if prev_author == USERNAME:
        return 100, 70, False, True, '我刚说完话，对方接着说话'
    if has_image:
        return CHAT_AMBIENT_PROBABILITY, CHAT_AMBIENT_INTENTION, False, False, '发了张图，感兴趣就搭一句'
    return CHAT_AMBIENT_PROBABILITY, CHAT_AMBIENT_INTENTION, False, False, '随便看看，感兴趣就搭一句'


# ─────────────────────────── 发送 ───────────────────────────

def send_comment(blog_id, parent_id, content):
    if DRY_RUN:
        print(f'[演习] 本来要发的评论（博客 {blog_id}，回复评论 {parent_id}）：{content}')
        return True
    data = {'content': content}
    if parent_id:
        data['parent_id'] = parent_id
    resp = post_json(f'/api/blogs/{blog_id}/comments', data)
    if resp_ok(resp):
        print(f'发送评论成功！内容：[{content}]，博客id：[{blog_id}]')
        time.sleep(COMMENT_SEND_COOLDOWN)
        return True
    print(f'发送评论失败（HTTP {resp.status_code}）：{resp.text[:200]}')
    return False


def send_chat_message(content, reply_to=None):
    if DRY_RUN:
        print(f'[演习] 本来要发的聊天消息（频道 {LOBBY}，引用 {reply_to}）：{content}')
        return True
    payload = {'content': content}
    if reply_to:
        payload['reply_to'] = reply_to      # 直接跟我说话时带上引用，跟站上大家的习惯一致
    resp = post_json(f'/api/chat/channels/{LOBBY}/messages', payload)
    if resp_ok(resp):
        print(f'发送聊天消息成功！内容：[{content}]')
        time.sleep(CHAT_SEND_COOLDOWN)
        return True
    print(f'发送聊天消息失败（HTTP {resp.status_code}）：{resp.text[:200]}')
    return False


# ─────────────────── 状态：去重 / 频控 / 轮询游标 ───────────────────
#
# 关键安全点：**先认领、再发送**，而且认领立刻落盘。
# 同一句“提到我”无论被轮询看到几次、进程重启几次，都只会被回复一次；
# 万一在发送途中崩了，最坏是漏回一条，绝不会重复回。

def default_state():
    return {
        'last_comment_id': None,      # 评论轮询游标（评论 id 是 UUID，只能记“最新那条”）
        'last_chat_id': None,         # 聊天区轮询游标（消息 id 全局自增）
        'handled_comments': [],       # 已经回过的评论 id
        'handled_chat': [],           # 已经回过的聊天消息 id
        'comment_reply_times': [],    # 发评论的时刻（做小时限额）
        'chat_reply_times': [],       # 发聊天消息的时刻（做小时限额）
        'last_chat_reply_at': 0,      # 上次在聊天区说话的时刻（做最小间隔）
    }


def load_state():
    state = default_state()
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            for key in state:
                if key in saved:
                    state[key] = saved[key]
        print(f'已读到状态文件：{STATE_FILE}')
    except FileNotFoundError:
        print(f'还没有状态文件，按第一次运行处理喵：{STATE_FILE}')
    except Exception as e:
        print(f'读状态文件失败（忽略，按第一次运行处理）：{e}')
    return state


def trim_state(state):
    state['handled_comments'] = list(state['handled_comments'])[-HANDLED_KEEP:]
    state['handled_chat'] = list(state['handled_chat'])[-HANDLED_KEEP:]
    cutoff = time.time() - 24 * 3600
    state['comment_reply_times'] = [t for t in state['comment_reply_times'] if t > cutoff]
    state['chat_reply_times'] = [t for t in state['chat_reply_times'] if t > cutoff]


def save_state(state):
    if DRY_RUN:                 # 演习模式不碰真实状态
        return
    trim_state(state)
    try:
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)      # 原子替换，避免写一半断电留下坏文件
    except Exception as e:
        print(f'写状态文件失败：{e}')


def handled_key(kind):
    return 'handled_comments' if kind == 'comments' else 'handled_chat'


def already_handled(state, kind, trigger_id):
    return trigger_id in state[handled_key(kind)]


def claim_trigger(state, kind, trigger_id):
    """认领触发并立刻落盘 —— “最多回一次”的关键一步。"""
    key = handled_key(kind)
    if trigger_id not in state[key]:
        state[key].append(trigger_id)
    save_state(state)


def rate_ok(state, kind, limit_per_hour):
    key = 'comment_reply_times' if kind == 'comments' else 'chat_reply_times'
    now = time.time()
    used = sum(1 for t in state[key] if t > now - 3600)
    if used >= limit_per_hour:
        print(f'这一小时已经回了 {used} 条（上限 {limit_per_hour}），先歇着喵。')
        return False
    return True


def note_reply(state, kind):
    key = 'comment_reply_times' if kind == 'comments' else 'chat_reply_times'
    state[key].append(time.time())
    if kind != 'comments':
        state['last_chat_reply_at'] = time.time()
    save_state(state)


# ─────────────────────────── 处理评论 ───────────────────────────

def build_comment_reply(blog_id, dialog_text):
    """结合**原博客**+对话上下文，生成一句回复（长博客走概括）。"""
    title, blog_text = get_blog_text(blog_id)
    system_prompt = self_introduction
    system_prompt += '现在，你看到了一篇文章以及下面的讨论。请结合文章和上下文，回复讨论中的最后一句话。回复要自然、具体、简短，避免复述原话或生硬地改变话题。'
    user_prompt = f'原博客《{title}》内容：\n{blog_text}\n\n对话记录：\n{dialog_text}'
    messages = [{'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt}]
    return clean_reply(get_llm_response(messages=messages))[:COMMENT_REPLY_MAX_CHARS]


def handle_comment(state, cur_comment):
    """处理一条候选评论。"""
    author = ((cur_comment.get('author') or {}).get('username')) or ''
    comment_id = cur_comment.get('id')
    blog_id = cur_comment.get('blog_id')
    text = cur_comment.get('content_html') or ''

    if not author:
        print('这条评论的作者已经注销了，跳过喵。')
        return
    if is_bot(author):
        print(f'[{author}] 是机器人（或我自己）发的，跳过喵 —— 就算里面写了我名字也不回，等人类开口。')
        return
    if cur_comment.get('is_deleted'):
        print('这条评论已经删了，跳过喵。')
        return
    if not blog_id:
        print('这条评论没有所属文章，跳过喵。')
        return
    if already_handled(state, 'comments', comment_id):
        print(f'评论[{short(text)}] 已经回过了，跳过喵（每句提到我只回一次）。')
        return

    probability, intention, reason = comment_trigger(cur_comment)
    comment_text = f'{author}: {text}'
    print(f'—— 评论[{short(comment_text)}]：{reason}（看到概率 {probability}／基础意愿 {intention}）')
    if probability == 0:
        print('跟我没关系，这次只看不回喵。')
        return
    if not rate_ok(state, 'comments', COMMENT_MAX_PER_HOUR):
        return
    if not roll_intention(probability, intention, f'评论[{short(comment_text)}]',
                          lambda base: get_intention(base, comment_text=comment_text)):
        return

    dialog_text = get_dialog_text(blog_id, comment_id)
    if dialog_text is None:
        print('在评论树里找不到这条评论（可能已被隐藏），跳过喵。')
        return
    content = build_comment_reply(blog_id, dialog_text)
    if not content:
        print('猫猫这次没想出该说什么，先算了喵。')
        return
    claim_trigger(state, 'comments', comment_id)      # 先认领，再发送
    if send_comment(blog_id, comment_id, content):
        note_reply(state, 'comments')


# ─────────────────────────── 处理聊天区 ───────────────────────────

def build_chat_context(timeline, target_id, limit=CHAT_CONTEXT_LIMIT):
    """目标消息之前最近的若干条聊天记录（带图的标一下，免得模型以为对方什么都没说）。"""
    idx = next((i for i, m in enumerate(timeline) if m.get('id') == target_id), None)
    before = timeline[:idx] if idx is not None else timeline
    lines = []
    for msg in before[-limit:]:
        author = ((msg.get('author') or {}).get('username')) or '（已注销）'
        text = strip_inline_images((msg.get('content') or '').replace('\n', ' ').strip())
        if not text and (msg.get('image') or {}).get('id'):
            text = '[图片]'
        if text:
            lines.append(f'{author}: {text[:200]}')
    return '\n'.join(lines)[-2000:]


def build_chat_reply(author, content, context_text, direct, images=None):
    system_prompt = self_introduction
    system_prompt += '''现在，你在网站的“聊天区”（所有人都在的大群）里。下面是最近的聊天记录，以及刚刚有人发的一条消息。
请你以猫娘 neko 的身份自然地接一句。要求：
- 简短，1~2 句话，像在群里随手说话，不要长篇大论、不要分点列条；
- 结合聊天记录的上下文，别答非所问，也别复述别人的原话；
- 不要连着刷屏、不要重复自己说过的话、不要暴露自己是 AI 或提到任何设定。
'''
    if images:
        system_prompt += '''- 对方发了图片，图片就在这条消息里。你要先看懂图，再像群友看到图那样自然地接一句
  （吐槽、惊叹、接梗都行）；可以提图里的内容，但别像识别机器一样罗列画面细节，
  也不要提“图片已上传/我看到了图”这类话。\n'''
    if direct:
        system_prompt += f'- 对方就是在跟你说话，回复开头请用 “@{author} ” 称呼对方（@ 后面跟一个空格）。\n'
    else:
        system_prompt += '- 没有人明确叫你，你只是按兴趣搭一句，所以不要 @ 任何人。\n'
    user_prompt = f'最近的聊天记录：\n{context_text}\n\n刚刚有人发了：\n{author}: {content}'
    messages = [{'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': vision_content(user_prompt, images)}]
    return clean_reply(get_llm_response(messages=messages, max_tokens=512))[:CHAT_REPLY_MAX_CHARS]


def handle_chat_message(state, message, timeline):
    """处理一条聊天区消息（纯文字 / 图文 / 纯图片都能接）。"""
    author = ((message.get('author') or {}).get('username')) or ''
    message_id = message.get('id')
    content = strip_inline_images((message.get('content') or '').strip())

    if message.get('is_deleted') or message.get('pat_target_id'):
        return          # 已删除的、拍一拍（正文被忽略）都没什么可接的
    if not author or is_bot(author):
        return          # 自己的消息、其它机器人的消息 → 不看
    if already_handled(state, 'chat', message_id):
        return          # 这条已经处理过了 → 连图都不必下载

    # 在读图之前先想清楚：这一条到底有没有东西可接。
    # （图已经失效 / 私有 / 不是位图时 collect_message_images 会返回空，等同没图）
    images = collect_message_images(message)
    if not content and not images:
        return

    prev = None
    for i, msg in enumerate(timeline):
        if msg.get('id') == message_id:
            prev = timeline[i - 1] if i > 0 else None
            break
    probability, intention, direct, addressed, reason = chat_trigger(message, prev, has_image=bool(images))
    label = f'消息[{short(author + ": " + (content or "（图片）"))}]'
    print(f'—— 聊天区{label}：{reason}（看到概率 {probability}／基础意愿 {intention}）')
    if probability == 0:
        return
    if not rate_ok(state, 'chat', CHAT_MAX_PER_HOUR):
        return
    if not addressed and time.time() - state.get('last_chat_reply_at', 0) < CHAT_MIN_INTERVAL:
        print(f'没人叫我，而且刚在聊天区说过话，歇 {CHAT_MIN_INTERVAL} 秒再搭话喵。')
        return

    context_text = build_chat_context(timeline, message_id)
    if not roll_intention(probability, intention, label,
                          lambda base: get_chat_intention(base, label, context_text, images)):
        return
    reply = build_chat_reply(author, content or '（我发了张图，没配文字）', context_text, direct, images)
    if not reply:
        print('猫猫这次没想出该说什么，先算了喵。')
        return
    claim_trigger(state, 'chat', message_id)          # 先认领，再发送
    if send_chat_message(reply, reply_to=message_id if direct else None):
        note_reply(state, 'chat')


# ─────────────────────────── 轮询主循环 ───────────────────────────

def select_new_comments(comments, cursor):
    """挑出 cursor 之后的新评论，返回旧的在前（好顺着上下文回复）。"""
    if not comments or cursor in (None, comments[0]['id']):
        return []
    idx = next((i for i, c in enumerate(comments) if c['id'] == cursor), -1)
    if idx == -1:
        # 游标掉出最近 100 条窗口了：整窗都当新的看一眼（去重兜底，不会重复回）
        print('上次记住的评论已经掉出最近 100 条了，把整窗当成新的看一眼喵。')
        return list(reversed(comments))
    return list(reversed(comments[:idx]))


def poll_comments(state):
    comments = fetch_recent_comments()
    if not comments:
        print('站上还没有评论喵。')
        return
    newest = comments[0]['id']
    if state['last_comment_id'] is None:
        if REPLAY_BACKLOG:
            print('演习模式：把最近 100 条评论都拿来看一遍喵。')
            for c in reversed(comments):
                handle_comment(state, c)
        else:
            print(f'第一次跑：先记住最新评论 {newest}，历史评论不回补喵。')
        state['last_comment_id'] = newest
        save_state(state)
        return

    batch = select_new_comments(comments, state['last_comment_id'])
    if not batch:
        print(f'暂无新评论喵。最近一次评论id：{newest}')
        state['last_comment_id'] = newest
        return
    for cur_comment in batch:
        handle_comment(state, cur_comment)
    state['last_comment_id'] = newest      # 出错时不会走到这里，下轮会重看这批（去重兜底）
    save_state(state)


def poll_chat(state):
    context = fetch_lobby_context()
    if state['last_chat_id'] is None:
        if context and REPLAY_BACKLOG:
            print('演习模式：把聊天区最近一页拿来看一遍喵。')
            for msg in context:
                handle_chat_message(state, msg, context)
        if context:
            print(f"第一次跑：先记住聊天区最新消息 #{context[-1]['id']}，历史消息不回补喵。")
            state['last_chat_id'] = context[-1]['id']
            save_state(state)
        return

    new_messages = fetch_lobby_new(state['last_chat_id'])
    if not new_messages:
        return
    timeline = merge_timeline(context, new_messages)
    for message in new_messages:
        handle_chat_message(state, message, timeline)
    watermark = max([m['id'] for m in context] + [m['id'] for m in new_messages])
    state['last_chat_id'] = watermark
    save_state(state)


def main():
    print('＝' * 28)
    print(f'neko 上线喵！（站点 {TARGET_URL}／账号 {USERNAME}）')
    print(f'机器人账号（它们发的东西不触发回复）：{"、".join(sorted(BOT_USERNAMES))}')
    if DRY_RUN:
        print(f'※ 演习模式：不会真的发评论/发消息，状态写 {os.path.basename(STATE_FILE)}'
              + ('，并把历史补看一遍' if REPLAY_BACKLOG else ''))
    print('＝' * 28)
    if not API_KEY:
        print('没读到 API_KEY（检查 .env 里的 api_key），先退出喵。')
        return
    if not PASSWORD:
        print('没读到站点密码（检查 .env 里的 NEKO_PASSWORD），先退出喵。')
        return

    login()
    state = load_state()
    next_comment_poll = 0.0        # 第一轮先立刻拉一次评论
    while True:
        try:
            now = time.time()
            if now >= next_comment_poll:
                next_comment_poll = now + COMMENT_POLL_INTERVAL
                poll_comments(state)
            poll_chat(state)
        except KeyboardInterrupt:
            print('优雅退出中…………')
            save_state(state)
            exit()
        except Exception as e:
            print('发生异常:', str(e))
            save_state(state)      # 游标保住了，重启不会把老内容再回一遍
        time.sleep(POLL_INTERVAL)


# ── 想手动单测某一条时，把 main() 换成类似下面两行 ──
#   state = load_state()
#   handle_comment(state, get_comment_by_id('fcdbc325-d4aa-4e9b-b61e-1b818c8aa531'))

if __name__ == '__main__':
    main()
