"""Bounded SQS delivery against an explicitly owned local emulator queue."""

from typing import Any
from urllib.parse import urlsplit

from app.adapters.io import blocking_call
from app.ports.queue import Delivery


class SQSQueue:
    def __init__(self, client: Any, url: str):
        parsed = urlsplit(url)
        if (
            client.meta.endpoint_url != "http://127.0.0.1:4567"
            or parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port != 4567
            or "/codespy-p2-test-" not in parsed.path
        ):
            raise ValueError("owned loopback queue required")
        self.client = client
        self.url = url

    async def send(self, body: bytes) -> None:
        if not body or len(body) > 4096:
            raise ValueError("bounded queue message required")
        text = body.decode("utf-8")
        await blocking_call(lambda: self.client.send_message(QueueUrl=self.url, MessageBody=text))

    async def receive(self, max_messages: int = 1, *, wait_seconds: int = 20) -> list[Delivery]:
        if not 1 <= max_messages <= 2 or not 0 <= wait_seconds <= 20:
            raise ValueError("invalid receive bounds")
        result = await blocking_call(
            lambda: self.client.receive_message(
                QueueUrl=self.url,
                MaxNumberOfMessages=max_messages,
                WaitTimeSeconds=wait_seconds,
                VisibilityTimeout=120,
            )
        )
        return [
            Delivery(body=m["Body"].encode(), receipt=m["ReceiptHandle"])
            for m in result.get("Messages", [])
        ]

    async def extend(self, receipt: str, seconds: int) -> None:
        if not 0 <= seconds <= 120:
            raise ValueError("invalid visibility bound")
        await blocking_call(
            lambda: self.client.change_message_visibility(
                QueueUrl=self.url, ReceiptHandle=receipt, VisibilityTimeout=seconds
            )
        )

    async def ack(self, receipt: str) -> None:
        await blocking_call(
            lambda: self.client.delete_message(QueueUrl=self.url, ReceiptHandle=receipt)
        )
