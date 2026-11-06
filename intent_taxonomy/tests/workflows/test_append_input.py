from intent_engine.workflows.append_input import plan_append


def test_append_deduplicates_against_existing_and_file_with_provenance():
    rows=[{'row_id':8,'raw_text':' existing '}]
    additions,stats=plan_append(rows,['existing',' new ','new','another','existing','',None,float('nan')])
    assert additions==[(9,' new ',2),(10,'another',4)]
    assert stats==dict(input_rows=8,row_count=2,duplicate_existing=2,duplicate_in_file=1,blank_rows=3,duplicate_rows=3)
    assert rows==[{'row_id':8,'raw_text':' existing '}]


def test_append_keeps_case_punctuation_and_meaning_differences():
    additions,_=plan_append([],['Pay','pay','pay?',' pay ','pay  bill','pay bill'])
    assert [a[1] for a in additions]==['Pay','pay','pay?','pay  bill','pay bill']


def test_append_same_file_again_adds_nothing():
    additions,_=plan_append([],['one','two','one'])
    rows=[{'row_id':i,'raw_text':text} for i,text,_ in additions]
    new,stats=plan_append(rows,['one','two','one'])
    assert new==[] and stats['duplicate_existing']==3
