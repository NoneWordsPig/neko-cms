"""Image extraction and bounded preprocessing for multimodal prompts."""

import base64
import io
import re


class ImageProcessor:
    def __init__(self, fetch_bytes, image_library, enabled=True, max_bytes=8 * 1024 * 1024,
                 max_dimension=768, max_frames=4, max_per_message=3, cache_size=16,
                 cache=None):
        self.fetch_bytes = fetch_bytes
        self.image_library = image_library
        self.enabled = enabled and image_library is not None
        self.max_bytes = max_bytes
        self.max_dimension = max_dimension
        self.max_frames = max_frames
        self.max_per_message = max_per_message
        self.cache_size = cache_size
        self.cache = cache if cache is not None else {}

    def to_data_url(self, data, mime):
        if not self.enabled or not data or len(data) > self.max_bytes:
            return None
        if mime and not mime.lower().startswith('image/'):
            return None
        if (mime or '').lower() == 'image/svg+xml':
            return None
        try:
            source = self.image_library.open(io.BytesIO(data))
            frame_count = getattr(source, 'n_frames', 1)
            frame_indexes = list(range(min(frame_count, self.max_frames)))
            frames = []
            for frame_index in frame_indexes:
                source.seek(frame_index)
                frame = source.convert('RGB')
                side = max(64, self.max_dimension // max(1, len(frame_indexes)))
                frame.thumbnail((side, side))
                frames.append(frame.copy())
            if not frames:
                return None
            if len(frames) == 1:
                result = frames[0]
            else:
                width = max(frame.width for frame in frames)
                height = sum(frame.height for frame in frames)
                result = self.image_library.new('RGB', (width, height), 'white')
                top = 0
                for frame in frames:
                    result.paste(frame, ((width - frame.width) // 2, top))
                    top += frame.height
            output = io.BytesIO()
            result.save(output, format='JPEG', quality=82, optimize=True)
            encoded = base64.b64encode(output.getvalue()).decode('ascii')
            return f'data:image/jpeg;base64,{encoded}'
        except Exception as error:
            print('图片解码失败（当作没看到）：', str(error)[:120])
            return None

    def data_url(self, image_id):
        if image_id in self.cache:
            result = self.cache.pop(image_id)
            self.cache[image_id] = result
            return result
        data, mime = self.fetch_bytes(image_id)
        result = self.to_data_url(data, mime)
        self.cache[image_id] = result
        while len(self.cache) > self.cache_size:
            self.cache.pop(next(iter(self.cache)))
        return result

    @staticmethod
    def message_image_ids(message):
        ids = []
        attached = message.get('image') or {}
        if attached.get('id') and not message.get('image_missing'):
            ids.append(attached['id'])
        ids.extend(re.findall(r'\[@([A-Za-z0-9]{10})\]', message.get('content') or ''))
        reply = message.get('reply') or {}
        match = re.search(r'/api/images/([A-Za-z0-9_-]{6,32})/raw', reply.get('image_url') or '')
        if match:
            ids.append(match.group(1))
        return list(dict.fromkeys(ids))

    def collect(self, message):
        if not self.enabled:
            return []
        images = []
        for image_id in self.message_image_ids(message)[:self.max_per_message]:
            result = self.data_url(image_id)
            if result:
                images.append(result)
        return images

    @staticmethod
    def prompt_content(text, images):
        if not images:
            return text
        return [
            {'type': 'text', 'text': text},
            *({'type': 'image_url', 'image_url': {'url': url}} for url in images),
        ]
