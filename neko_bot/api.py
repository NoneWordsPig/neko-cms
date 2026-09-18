"""HTTP gateway for the Raricy site API."""


class SiteClient:
    def __init__(self, base_url, username, password, session, timeout=20, on_login=None):
        self.base_url = base_url.rstrip('/')
        self.username = username
        self.password = password
        self.session = session
        self.timeout = timeout
        self.on_login = on_login or (lambda user: None)

    def login(self):
        response = self.session.post(
            f'{self.base_url}/api/auth/login',
            json={'username': self.username, 'password': self.password},
            timeout=self.timeout,
        )
        try:
            data = response.json()
        except ValueError as error:
            raise RuntimeError(
                f'登录返回的不是 JSON（HTTP {response.status_code}）：{response.text[:200]}'
            ) from error
        if response.status_code != 200 or data.get('code') != 200:
            raise RuntimeError(f'登录失败（HTTP {response.status_code}）：{data}')
        user = data.get('user') or {}
        self.on_login(user)
        print(f"登录成功喵：{user.get('username')}（角色 {user.get('role')}）")
        return user

    def request(self, method, path, **kwargs):
        kwargs.setdefault('timeout', self.timeout)
        response = self.session.request(method, f'{self.base_url}{path}', **kwargs)
        if response.status_code == 401:
            print('会话过期了喵，重新登录……')
            self.login()
            response = self.session.request(method, f'{self.base_url}{path}', **kwargs)
        return response

    def get_json(self, path):
        response = self.request('GET', path)
        if response.status_code != 200:
            raise RuntimeError(
                f'GET {path} 失败（HTTP {response.status_code}）：{response.text[:200]}'
            )
        try:
            return response.json()
        except ValueError as error:
            raise RuntimeError(f'GET {path} 返回的不是 JSON：{response.text[:200]}') from error

    def post_json(self, path, payload):
        return self.request('POST', path, json=payload)

    @staticmethod
    def response_ok(response):
        if response.status_code != 200:
            return False
        try:
            return (response.json() or {}).get('code') == 200
        except ValueError:
            return False
