"""Deduplicate appended sentences before extraction; preserve original provenance."""
import math


def plan_append(existing_rows, values):
    existing = {str(row['raw_text']).strip() for row in existing_rows}
    seen = set(existing)
    offset = max((row['row_id'] for row in existing_rows),default=0)
    additions=[]
    stats={'input_rows':0,'row_count':0,'duplicate_existing':0,'duplicate_in_file':0,'blank_rows':0}
    for source_row,value in enumerate(values,1):
        stats['input_rows']+=1
        text='' if value is None or (isinstance(value,float) and math.isnan(value)) else str(value)
        key=text.strip()
        if not key:
            stats['blank_rows']+=1
        elif key in existing:
            stats['duplicate_existing']+=1
        elif key in seen:
            stats['duplicate_in_file']+=1
        else:
            seen.add(key)
            additions.append((offset+len(additions)+1,text,source_row))
    stats['row_count']=len(additions)
    stats['duplicate_rows']=stats['duplicate_existing']+stats['duplicate_in_file']
    return additions,stats
