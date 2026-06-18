from __future__ import annotations


class MspError(Exception):
    status_code = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ConflictError(MspError):
    status_code = 409


class SafetyError(MspError):
    status_code = 422


class NotFoundError(MspError):
    status_code = 404


class IdempotencyError(MspError):
    status_code = 428
