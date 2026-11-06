"""Read a bounded log tail without scanning a task's full history."""
import json


def read_log_tail(path, limit=2000, max_bytes=2*1024*1024):
    if limit<1 or max_bytes<1:raise ValueError("日志读取上限必须大于零")
    blocks=[]
    with path.open('rb') as stream:
        stream.seek(0,2)
        position=stream.tell();newlines=0;remaining=max_bytes
        while position and newlines<=limit and remaining:
            size=min(65536,position,remaining);position-=size;remaining-=size;stream.seek(position)
            block=stream.read(size);blocks.append(block);newlines+=block.count(b'\n')
    data=b''.join(reversed(blocks))
    if position:
        # The read window may begin inside a UTF-8 sequence or JSON string.
        newline=data.find(b'\n')
        data=data[newline+1:] if newline>=0 else b''
    lines=data.splitlines()[-limit:]
    values=[]
    for line in lines:
        try:
            value=json.loads(line)
            if isinstance(value,str):values.append(value)
        except (ValueError,UnicodeError):continue
    return values
