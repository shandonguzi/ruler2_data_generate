"""Explicitly invoked live test. No imports or execution from the automatic test suite."""
import argparse
import getpass
import json
import os
import sys
import shlex
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


ENV_KEYS={'EVAL_VLLM_BASE_URL','EVAL_VLLM_MODEL','EVAL_VLLM_API_KEY','INTENT_MANUAL_TEST_ENABLE_THINKING','INTENT_MANUAL_TEST_BASE_URL','INTENT_MANUAL_TEST_MODEL','INTENT_MANUAL_TEST_API_KEY'}


def read_test_env(path):
    """Read the three single-line settings only; never execute or expand values."""
    values={}
    if not path.exists():return values
    for number,line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(),1):
        line=line.strip()
        if line.startswith('export '):line=line[7:].lstrip()
        key,separator,value=line.partition('=')
        key=key.strip()
        if not separator or key not in ENV_KEYS:continue
        try:
            parts=shlex.split(value,comments=True,posix=True)
        except ValueError:
            raise ValueError(f'.env 第 {number} 行格式错误（值未显示）') from None
        if len(parts)>1:raise ValueError(f'.env 第 {number} 行含空格的值请加引号')
        values[key]=parts[0] if parts else ''
    for suffix in ['BASE_URL','MODEL','API_KEY']:
        if 'EVAL_VLLM_'+suffix in values:values['INTENT_MANUAL_TEST_'+suffix]=values['EVAL_VLLM_'+suffix]
    return values


def test_setting(settings,key,default=None):
    alias=key.replace('INTENT_MANUAL_TEST_','EVAL_VLLM_')
    return settings.get(key,os.environ.get(alias,os.environ.get(key,default)))


def thinking_body(settings):
    value=settings.get('INTENT_MANUAL_TEST_ENABLE_THINKING',os.environ.get('INTENT_MANUAL_TEST_ENABLE_THINKING','false')).lower()
    if value not in {'true','false'}:raise ValueError('INTENT_MANUAL_TEST_ENABLE_THINKING 必须为 true 或 false')
    from urllib.parse import urlsplit
    base=test_setting(settings,'INTENT_MANUAL_TEST_BASE_URL','')
    if urlsplit(base).hostname=='api.deepseek.com':
        return {'thinking':{'type':'enabled' if value=='true' else 'disabled'}}
    return {'enable_thinking':value=='true'}


def main():
    parser=argparse.ArgumentParser(description='手动真实模型测试：每个示例恰好一次请求，不重试；默认1次，all最多3次。')
    parser.add_argument('--case',choices=['retain','parent','normalize','all'],default='retain')
    parser.add_argument('--base-url',default=None)
    parser.add_argument('--model',default=None)
    args=parser.parse_args()
    try:settings=read_test_env(Path(__file__).resolve().parents[1]/'.env')
    except ValueError as exc:parser.error(str(exc))
    def setting(key,default=None):return test_setting(settings,key,default)
    args.base_url=args.base_url or setting('INTENT_MANUAL_TEST_BASE_URL','https://dashscope.aliyuncs.com/compatible-mode/v1')
    extra_body=thinking_body({**settings,'INTENT_MANUAL_TEST_BASE_URL':args.base_url})
    args.model=args.model or setting('INTENT_MANUAL_TEST_MODEL','GLM-5.2')
    from openai import OpenAI
    from intent_engine.atomic.cluster_refinement import FullPartition, SPLIT_PROMPT, validate_partition
    from intent_engine.atomic.leaf_merge import MergeProposal, MERGE_PROMPT
    from intent_engine.settings.prompt_settings import resolve_prompt
    cases=['retain','parent','normalize'] if args.case=='all' else [args.case]
    print(f'计划实际 API 请求：{len(cases)} 次；每个示例1次，无失败重试、无Embedding、无流程重建。')
    key=setting('INTENT_MANUAL_TEST_API_KEY') or getpass.getpass('请输入测试 API Key（不回显、不保存）：')
    if not key:parser.error('API Key 不能为空')
    client=OpenAI(base_url=args.base_url,api_key=key,max_retries=0,timeout=120)
    sent=0;failed=0
    try:
        for case in cases:
            if case=='retain':
                members=[{'intent_id':'a','text':'What is the official religion of Jordan?'},{'intent_id':'b','text':'What is the official religion of Morocco?'}]
                payload={'members':members,'topic_context':{'source':'unavailable','path':[]},'review':{'verdict':'mixed','reason':'此前建议分开，现复核'},'candidate_groups':[]}
                prompt,schema=SPLIT_PROMPT,FullPartition
            else:
                left,right=(('What material is a jambiya made from?','How is a jambiya made?') if case=='parent' else ('What material is this garment made from?','Which materials are used to make this garment?'))
                payload={'left':{'label':'材料查询','members':[{'text':left}]},'right':{'label':'制作查询','members':[{'text':right}]},'conflict_texts':[]}
                prompt,schema=MERGE_PROMPT,MergeProposal
            try:
                sent+=1
                response=client.chat.completions.create(model=args.model,temperature=0.1,max_tokens=1600,extra_body=extra_body,
                    messages=[{'role':'system','content':resolve_prompt(prompt)},{'role':'user','content':json.dumps(payload,ensure_ascii=False)}])
                parsed=schema.model_validate_json(response.choices[0].message.content)
                if case=='retain':validate_partition(parsed,members)
                print(case,parsed.model_dump_json(indent=2))
                if response.usage:print('tokens:',response.usage.total_tokens)
            except Exception as exc:
                failed+=1;print(f'{case} 失败（不重试）：{str(exc).replace(key,"[REDACTED]")}')
    finally:
        client.close();print(f'实际请求：{sent} 次，失败：{failed} 次。')
    return bool(failed)


if __name__=='__main__':raise SystemExit(main())
