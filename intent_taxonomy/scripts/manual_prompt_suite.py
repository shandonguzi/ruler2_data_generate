"""Manual-only live prompt suite; preview by default, at most three calls per run."""
import argparse
import getpass
import json
import os
import sys
import time
import uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.manual_semantic_model_test import read_test_env,thinking_body,test_setting
from scripts.prompt_test_cases import cases,check_response


def select_cases(all_cases,prompts=(),batch=None,examples=1,max_requests=3):
    if batch is not None:
        if prompts:raise ValueError('--batch 和 --prompt 不能同时使用')
        if batch<1:raise ValueError('批次从1开始')
        chosen=all_cases[(batch-1)*max_requests:batch*max_requests]
    else:
        unknown=set(prompts)-{c['prompt'] for c in all_cases}
        if unknown:raise ValueError('未知Prompt：'+', '.join(sorted(unknown)))
        chosen=[c for key in dict.fromkeys(prompts) for c in [x for x in all_cases if x['prompt']==key][:examples]]
    if not chosen:raise ValueError('没有选中用例，请选择 --prompt 或 --batch')
    if len(chosen)>max_requests:raise ValueError(f'本次选择{len(chosen)}条，超过{max_requests}次请求上限，请分批运行')
    return chosen


def main():
    parser=argparse.ArgumentParser(description='17个Prompt逐项手动测试；默认仅预览，加--run才请求，每次最多3次，不重试。')
    parser.add_argument('--dataset',choices=['real','examples'],default='real',help='默认使用真实报告挑选集；examples为原人工示例集')
    parser.add_argument('--list',action='store_true')
    parser.add_argument('--prompt',action='append',default=[])
    parser.add_argument('--examples',type=int,choices=[1,2],default=1)
    parser.add_argument('--batch',type=int)
    parser.add_argument('--max-requests',type=int,choices=[1,2,3],default=3)
    parser.add_argument('--run',action='store_true')
    args=parser.parse_args()
    fixtures=json.loads((ROOT/'tests/fixtures/real_prompt_cases.json').read_text(encoding='utf-8'))['cases'] if args.dataset=='real' else cases()
    if args.list or (not args.prompt and args.batch is None):
        for i,c in enumerate(fixtures):print(f'批次{i//args.max_requests+1:02d} | {c["id"]} | {c["focus"]}')
        print(f'{len(set(c["prompt"] for c in fixtures))}个Prompt，{len(fixtures)}条用例；全覆盖需{len(fixtures)}次请求，每批最多{args.max_requests}次。当前未请求。')
        return 0
    try:
        chosen=select_cases(fixtures,args.prompt,args.batch,args.examples,args.max_requests)
        settings=read_test_env(ROOT/'.env');extra=thinking_body(settings)
    except ValueError as exc:parser.error(str(exc))
    for c in chosen:print(c['id'],c['focus'],f'输入约{len(json.dumps(c["input"],ensure_ascii=False))}字符')
    print(f'计划请求{len(chosen)}次，无重试；extra_body={json.dumps(extra)}。')
    if not args.run:
        print('预览结束，实际请求0次；添加 --run 才执行。');return 0
    from openai import OpenAI
    from intent_engine.settings.prompt_settings import prompt_ref,resolve_prompt
    setting=lambda k,default=None:test_setting(settings,k,default)
    key=setting('INTENT_MANUAL_TEST_API_KEY') or getpass.getpass('API Key（不回显）：')
    if not key:parser.error('API Key不能为空')
    model=setting('INTENT_MANUAL_TEST_MODEL','glm-5.2')
    base=setting('INTENT_MANUAL_TEST_BASE_URL','https://dashscope.aliyuncs.com/compatible-mode/v1')
    report={'model':model,'base_url':base,'extra_body':extra,'requests':0,'failed':0,'cases':[]}
    out=ROOT/'data'/'reports'/'prompt_tests'/(time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]+'.json')
    from intent_engine.infrastructure.atomic_json import write_json_atomically
    client=OpenAI(base_url=base,api_key=key,max_retries=0,timeout=120)
    try:
        for case in chosen:
            record={**case,'status':'pending'};report['cases'].append(record);started=time.perf_counter()
            try:
                record['system_prompt']=resolve_prompt(prompt_ref(case['prompt']))
                report['requests']+=1
                response=client.chat.completions.create(model=model,temperature=.1,max_tokens=2400,extra_body=extra,
                    messages=[{'role':'system','content':record['system_prompt']},{'role':'user','content':json.dumps(case['input'],ensure_ascii=False)}])
                record['output']=response.choices[0].message.content
                record['usage']=response.usage.model_dump() if response.usage else None
                parsed,failures=check_response(case,json.loads(record['output']))
                record.update(parsed=parsed,expectation_failures=failures,status='mismatch' if failures else 'needs_review' if case.get('manual_review') else 'passed')
                if failures:report['failed']+=1
            except Exception as exc:
                report['failed']+=1;record.update(status='error',error=str(exc).replace(key,'[REDACTED]'))
            record['elapsed_seconds']=round(time.perf_counter()-started,4)
            report['needs_review']=sum(r.get('status')=='needs_review' for r in report['cases'])
            print(case['id'],record['status'],record.get('expectation_failures',record.get('error','')))
            write_json_atomically(out,report)
    finally:
        client.close()
        print(f'实际请求{report["requests"]}次，失败或不符预期{report["failed"]}条，待人工审核{report.get("needs_review",0)}条；记录：{out}')
    return int(bool(report['failed']))


if __name__=='__main__':raise SystemExit(main())
