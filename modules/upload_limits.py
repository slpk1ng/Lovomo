"""Streaming budgets shared by multipart upload endpoints."""
MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_PARTS = 32


class UploadLimitError(ValueError):
    pass


async def read_upload(part, request, limit=MAX_FILE_BYTES):
    budget = request.setdefault("lovomo_upload_budget", {"bytes": 0, "parts": 0, "seen": []})
    if part not in budget["seen"]:
        budget["seen"].append(part)
        budget["parts"] += 1
    if budget["parts"] > MAX_PARTS:
        raise UploadLimitError("一次上传最多允许 32 个字段或文件")
    data = bytearray()
    while True:
        chunk = await part.read_chunk(size=64 * 1024)
        if not chunk:
            break
        budget["bytes"] += len(chunk)
        if budget["bytes"] > MAX_REQUEST_BYTES or len(data) + len(chunk) > limit:
            raise UploadLimitError("上传大小超限：单文件最多 20MB，单请求最多 64MB")
        data.extend(chunk)
    return bytes(data)


async def read_field(part, request):
    return (await read_upload(part, request, limit=4096)).decode("utf-8")


async def bounded_multipart(request):
    if request.content_length and request.content_length > MAX_REQUEST_BYTES:
        raise UploadLimitError("单请求最多允许 64MB")
    reader = await request.multipart()
    async for part in reader:
        yield part
        # Drain unknown/unconsumed fields through the same budget before advancing.
        await read_upload(part, request)
