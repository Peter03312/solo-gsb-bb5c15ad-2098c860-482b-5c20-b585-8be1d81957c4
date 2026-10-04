"""统一的业务错误类型。"""


class ApiError(Exception):
    """所有可预期的拒绝场景都抛此异常，由 HTTP 层转成 JSON 响应。

    重要：任何写操作在校验失败时整体回滚，不改动历史。
    """

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
