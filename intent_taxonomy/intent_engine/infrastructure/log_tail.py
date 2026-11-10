"""Read a bounded log tail without scanning a task's full history."""
import json
import time


def _read_log_tail(path, limit, max_bytes, timing):
    if limit<1 or max_bytes<1:raise ValueError("日志读取上限必须大于零")
    blocks=[]
    with path.open('rb') as stream:
        stream.seek(0,2)
        position=stream.tell();newlines=0;remaining=max_bytes
        while position and newlines<=limit and remaining:
            size=min(65536,position,remaining);position-=size;remaining-=size;stream.seek(position)
            block=stream.read(size);blocks.append(block);newlines+=block.count(b'\n')
    timing['bytes']=sum(map(len,blocks))
    timing['read']=time.perf_counter()-timing['start']
    parse_start=time.perf_counter()
    data=b''.join(reversed(blocks))
    if position:
        # The read window may begin inside a UTF-8 sequence or JSON string.
        newline=data.find(b'\n')
        data=data[newline+1:] if newline>=0 else b''
    lines=data.splitlines()[-limit:]
    timing['lines']=len(lines)
    values=[]
    for line in lines:
        try:
            value=json.loads(line)
            if isinstance(value,str):values.append(value)
        except (ValueError,UnicodeError):continue
    timing['parse']=time.perf_counter()-parse_start
    timing['valid']=len(values)
    return values


def read_log_tail(path, limit=2000, max_bytes=2*1024*1024):
    """Print one timing summary per actual file load, never log file contents."""
    timing={'start':time.perf_counter(),'bytes':0,'lines':0,'valid':0,'read':0.,'parse':0.}
    status='失败'
    try:
        values=_read_log_tail(path,limit,max_bytes,timing)
        status='完成'
        return values
    finally:
        elapsed=time.perf_counter()-timing['start']
        print(f"[日志耗时] {path} | {status} | 读取={timing['read']:.4f}s "
              f"解析={timing['parse']:.4f}s 总计={elapsed:.4f}s | "
              f"读取字节={timing['bytes']} 行数={timing['lines']} 有效行={timing['valid']}",flush=True)
