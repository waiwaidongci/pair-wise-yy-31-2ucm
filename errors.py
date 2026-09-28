"""跨模块共享的 API 异常类型，独立成模块以避免 __main__/app 双份类定义。"""


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message
