import hashlib
import os
import tempfile
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from workerapp.core import Settings, Store, StoreError, VideoWorker
from workerapp.app import create_app

TOKEN="t"*48

@pytest.fixture
def settings(tmp_path):
    root=tmp_path/'upstream'
    root.mkdir()
    starter=tmp_path/'starter'
    (starter/'forwardkit').mkdir(parents=True)
    (starter/'qualifiedleads').mkdir()
    (starter/'scripts').mkdir()
    for brand in ('forwardkit','qualifiedleads'):
        (starter/'scripts'/f'{brand}.txt').write_text('This is a sample video script that has adequate length for testing.')
        for n in range(2):
            Image.new('RGB',(720,1280),'blue').save(starter/brand/f'scene-{n:02d}.png')
    return Settings(data_dir=tmp_path/'data',mpt_root=root,starter_dir=starter,api_token=TOKEN,
        min_free_bytes=0,poll_seconds=.03,job_timeout_seconds=30)

@pytest.fixture
def store(settings):
    return Store(settings)


def create(store,brand='forwardkit',**kwargs):
    return store.create(brand=brand,script='A nice short script for the first video generation.',
        voice_name='en-US-JennyNeural-Female',use_starter=True,auto_queue=True,idempotency_key=None,**kwargs)


def test_queue_is_durable_and_atomic(store,settings):
    row=create(store)
    assert row['state']=='queued'
    first=store.claim()
    assert first['id']==row['id']
    assert store.claim() is None
    second=Store(settings)
    assert second.get(row['id'])['state']=='running'
    assert first['attempts']==1
    assert second.heartbeat(row['id'],first['run_token'])


def test_retry_backoff_and_finish(store):
    row=create(store)
    claimed=store.claim()
    store.fail(row['id'], claimed['run_token'],'temporary failure')
    assert store.get(row['id'])['state']=='queued'
    with store.db() as db:
        db.execute("UPDATE jobs SET next_run_at=0 WHERE id=?",(row['id'],))
    claimed=store.claim()
    out=store.output_path(row['id'])
    out.write_bytes(b'\x00\x00\x00\x20ftypisom'*10)
    success=store.success(row['id'],claimed['run_token'],out)
    assert success['state']=='succeeded'
    assert len(success['sha256'])==64
    assert Store(store.s).get(row['id'])['state']=='succeeded'


def test_stale_lease_recovery(store):
    row=create(store)
    active=store.claim()
    with store.db() as db:
        db.execute('UPDATE jobs SET lease_until=0 WHERE id=?',(row['id'],))
    recovered=store.claim()
    assert recovered['id']==row['id']
    assert recovered['attempts']==2
    assert active['run_token']!=recovered['run_token']


def test_idempotency(store):
    one=store.create(brand='forwardkit',script='The first message script is sufficiently long.',
        voice_name='en-US-JennyNeural-Female',use_starter=True,auto_queue=True,idempotency_key='social-20261011-a')
    two=store.create(brand='forwardkit',script='The first message script is sufficiently long.',
        voice_name='en-US-JennyNeural-Female',use_starter=True,auto_queue=True,idempotency_key='social-20261011-a')
    assert one['id']==two['id']
    with pytest.raises(StoreError):
        store.create(brand='forwardkit',script='Different script but still sufficiently long.',
        voice_name='en-US-JennyNeural-Female',use_starter=True,auto_queue=True,idempotency_key='social-20261011-a')


def test_api_requires_auth(settings):
    app=create_app(settings,launch_worker=False)
    with TestClient(app) as client:
        assert client.get('/healthz').status_code==200
        assert client.get('/v1/jobs').status_code==401
        assert client.get('/v1/jobs',headers={'Authorization':'Bearer wrong'}).status_code==401
        headers={'Authorization':f'Bearer {TOKEN}'}
        assert client.get('/v1/jobs',headers=headers).json()==[]
        response=client.post('/v1/jobs',headers=headers,json={'brand':'forwardkit'})
        assert response.status_code==201,response.text
        jid=response.json()['id']
        assert response.json()['state']=='queued'
        assert client.get('/v1/jobs/'+jid,headers=headers).status_code==200
        assert client.get('/v1/jobs/'+jid+'/download',headers=headers).status_code==409
        assert client.get('/v1/jobs/'+jid+'/download').status_code==401
        assert client.post('/v1/jobs/'+jid+'/cancel',headers=headers).json()['state']=='canceled'


def test_api_draft_media_and_queue(settings,tmp_path):
    app=create_app(settings,launch_worker=False)
    with TestClient(app) as client:
        headers={'Authorization':f'Bearer {TOKEN}'}
        response=client.post('/v1/jobs',headers=headers,json={'brand':'qualifiedleads','use_starter':False,'auto_queue':False})
        assert response.status_code==201
        jid=response.json()['id']
        assert client.post(f'/v1/jobs/{jid}/enqueue',headers=headers).status_code==422
        image=tmp_path/'upload.png'
        Image.new('RGB',(720,1280),'green').save(image)
        with image.open('rb') as f:
            uploaded=client.post(f'/v1/jobs/{jid}/media',headers=headers,files={'file':('scene.png',f,'image/png')})
        assert uploaded.status_code==201,uploaded.text
        assert client.post(f'/v1/jobs/{jid}/enqueue',headers=headers).json()['state']=='queued'
        assert client.post(f'/v1/jobs/{jid}/media',headers=headers,files={'file':('evil.svg',b'<svg/>','image/svg+xml')}).status_code==422


def test_render_invocation_is_safe_and_atomic(settings,store,monkeypatch):
    # Simulate an upstream MoneyPrinterTurbo invocation and verify that the
    # real worker publishes a durable result without shell execution.
    root=settings.mpt_root
    (root/'.venv/bin').mkdir(parents=True)
    (root/'.venv/bin/python').write_text('fake')
    (root/'cli.py').write_text('fake')
    called=[]
    class FakeProc:
        pid=os.getpid()
        returncode=0
        def __init__(self,args,**kwargs):
            called.append(args)
            taskid=args[args.index('--task-id')+1]
            folder=root/'storage/tasks'/taskid
            folder.mkdir(parents=True,exist_ok=True)
            (folder/'final-1.mp4').write_bytes(b'fake MP4 bytes'*200)
        def poll(self): return self.returncode
        def wait(self,timeout=None): return self.returncode
    monkeypatch.setattr('workerapp.core.subprocess.Popen',FakeProc)
    queued=create(store)
    claimed=store.claim()
    worker=VideoWorker(store)
    worker.render(claimed)
    final=store.get(queued['id'])
    assert final['state']=='succeeded',final['last_error']
    assert store.output_path(queued['id']).is_file()
    assert called[0][called[0].index('--video-source')+1]=='local'
    assert '--no-subtitle-enabled' in called[0]


def test_no_secrets_in_job_data(settings):
    app=create_app(settings,launch_worker=False)
    with TestClient(app) as client:
        row=client.post('/v1/jobs',headers={'Authorization':f'Bearer {TOKEN}'},json={'brand':'forwardkit'}).json()
        assert TOKEN not in str(row)
        assert 'run_token' not in row
        assert 'task_id' not in row


def test_invalid_api_token_rejected(monkeypatch):
    monkeypatch.delenv('MPT_WORKER_API_TOKEN',raising=False)
    with pytest.raises(RuntimeError):Settings.from_env()


def test_manual_retry_and_deletion(settings):
    app=create_app(settings,launch_worker=False)
    token={'Authorization':f'Bearer {TOKEN}'}
    with TestClient(app) as client:
        row=client.post('/v1/jobs',headers=token,json={'brand':'forwardkit'}).json()
        jid=row['id']
        assert client.delete('/v1/jobs/'+jid,headers=token).status_code==409
        assert client.post(f'/v1/jobs/{jid}/cancel',headers=token).status_code==200
        assert client.delete('/v1/jobs/'+jid,headers=token).json()=={'deleted':True}
        assert client.get('/v1/jobs/'+jid,headers=token).status_code==404
