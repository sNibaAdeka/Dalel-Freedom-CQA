import pytest

from scripts.live_smoke import select_smoke, main


def test_smoke_requires_all_languages_and_a_call():
    convs=[{'id':str(i),'channel':channel,'language':lang} for i,(channel,lang) in
           enumerate([('chat','RU'),('chat','KZ'),('chat','EN'),('call','RU'),('chat','RU')])]
    assert [c['id'] for c in select_smoke(convs)] == ['0','1','2','3']
    with pytest.raises(ValueError,match='call/RU'):
        select_smoke(convs[:3])


def test_mock_cannot_be_reported_as_live(tmp_path, monkeypatch):
    monkeypatch.delenv('CQA_MODEL',raising=False)
    with pytest.raises(SystemExit) as exc:
        main(['--data',str(tmp_path/'absent.json'),'--profile','mock'])
    assert exc.value.code == 2
