"""Language-model transport and output normalization."""

import re


class ChatModel:
    def __init__(self, client, model='deepseek-flash'):
        self.client = client
        self.model = model

    def complete(self, messages, max_tokens=1024, temperature=0.7, json_output=False):
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=False,
            response_format={'type': 'json_object' if json_output else 'text'},
        )
        return response.choices[0].message.content

    @staticmethod
    def clean(text):
        text = (text or '').strip().strip('"\'“”')
        text = re.sub(r'^(?:neko)\s*[:：]\s*', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()
