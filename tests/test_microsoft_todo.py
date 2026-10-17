import asyncio
import json
import time
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from src import microsoft_todo as todo
from routes.device_flow import PendingDeviceFlowStore
from src.secret_storage import encrypt, decrypt

CLIENT='11111111-1111-4111-8111-111111111111'

@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setattr(todo,'STORE_FILE',tmp_path/'private'/'todo.json')
    monkeypatch.setattr(todo,'flows',PendingDeviceFlowStore())
    monkeypatch.setattr(todo,'_locks',{})
    monkeypatch.setattr(todo,'_active_flows',{})


def response(code,payload):return SimpleNamespace(status_code=code,json=lambda:payload)

def connected(owner='alice',expired=False):
    todo.save(owner,{'generation':'version','client_id':CLIENT,'default_list_id':'chosen',
                     'default_list_name':'Samsung','tokens':encrypt(json.dumps({
                         'access_token':'access-secret','refresh_token':'refresh-secret',
                         'expires_at':0 if expired else time.time()+3600}))})


def test_device_login_keeps_credentials_server_side_and_owner_bound(monkeypatch):
    calls=[]
    async def http(method,url,**kw):
        calls.append((url,kw))
        if url.endswith('/devicecode'):
            return response(200,{'device_code':'device-secret','user_code':'USER-CODE',
                                  'verification_uri':'https://evil.example','interval':5,'expires_in':900})
        return response(200,{'access_token':'access-secret','refresh_token':'refresh-secret',
                              'scope':'Tasks.ReadWrite','expires_in':3600})
    monkeypatch.setattr(todo,'_http',http)
    async def run():
        start=await todo.start_login('alice',CLIENT)
        assert 'device-secret' not in json.dumps(start)
        assert start['verification_uri']=='https://microsoft.com/devicelogin'
        with pytest.raises(HTTPException) as ex:await todo.poll_login('bob',start['poll_id'])
        assert ex.value.status_code==404
        assert (await todo.poll_login('alice',start['poll_id']))['status']=='connected'
        safe=todo.status('alice')
        assert safe['connected'] and not safe['default_list_id']
        assert 'secret' not in json.dumps(safe)
        raw=todo.STORE_FILE.read_text()
        assert 'access-secret' not in raw and 'refresh-secret' not in raw
        assert todo.STORE_FILE.stat().st_mode & 0o777 == 0o600
        assert not todo.status('bob')['connected']
    asyncio.run(run())
    assert 'offline_access' in calls[0][1]['data']['scope']


def test_poll_interval_and_disconnect_invalidate_flow(monkeypatch):
    calls=[]
    async def http(method,url,**kw):
        calls.append(url)
        return response(200,{'device_code':'secret','user_code':'CODE','verification_uri':'url'}) if url.endswith('devicecode') else response(400,{'error':'authorization_pending'})
    monkeypatch.setattr(todo,'_http',http)
    async def run():
        start=await todo.start_login('alice',CLIENT)
        await todo.poll_login('alice',start['poll_id'])
        await todo.poll_login('alice',start['poll_id'])
        assert len(calls)==2
        await todo.disconnect('alice')
        with pytest.raises(HTTPException) as ex:await todo.poll_login('alice',start['poll_id'])
        assert ex.value.status_code in (404,409)
        assert not todo.status('alice')['connected']
    asyncio.run(run())


def test_provider_errors_are_not_exposed(monkeypatch):
    async def http(*a,**kw):return response(400,{'error_description':'sensitive-provider-body'})
    monkeypatch.setattr(todo,'_http',http)
    with pytest.raises(HTTPException) as ex:asyncio.run(todo.start_login('alice',CLIENT))
    assert 'sensitive-provider-body' not in str(ex.value)


def test_refresh_is_rotated_once_for_concurrent_reads(monkeypatch):
    connected(expired=True);calls=[]
    async def http(method,url,**kw):
        calls.append((url,kw))
        await asyncio.sleep(0)
        if url.endswith('/token'):return response(200,{'access_token':'new-access','refresh_token':'rotated-refresh','expires_in':3600})
        assert kw['headers']['Authorization']=='Bearer new-access'
        return response(200,{'value':[]})
    monkeypatch.setattr(todo,'_http',http)
    async def run():await asyncio.gather(todo.tasks('alice',{'action':'list_tasks'}),todo.tasks('alice',{'action':'list_tasks'}))
    asyncio.run(run())
    assert sum(url.endswith('/token') for url,_ in calls)==1
    assert json.loads(decrypt(todo.record('alice')['tokens']))['refresh_token']=='rotated-refresh'


def test_crud_uses_selected_list_and_correct_time_zone(monkeypatch):
    connected();calls=[]
    async def http(method,url,**kw):
        calls.append((method,url,kw.get('json')))
        if method=='DELETE':return response(204,{})
        if method=='GET':return response(200,{'value':[{'id':'task','title':'Task','status':'notStarted'}]})
        return response(201 if method=='POST' else 200,{'id':'task','title':'Task','status':'completed' if kw.get('json',{}).get('status')=='completed' else 'notStarted'})
    monkeypatch.setattr(todo,'_http',http)
    async def run():
        await todo.tasks('alice',{'action':'create_task','title':'Task','remind_at':'2026-10-20T18:00:00+02:00'})
        await todo.tasks('alice',{'action':'list_tasks'})
        await todo.tasks('alice',{'action':'update_task','task_id':'task','notes':'Changed'})
        await todo.tasks('alice',{'action':'complete_task','task_id':'task'})
        assert (await todo.tasks('alice',{'action':'delete_task','task_id':'task'}))['deleted']
    asyncio.run(run())
    assert all('/me/todo/lists/chosen/tasks' in url for _,url,_ in calls)
    assert calls[0][2]['reminderDateTime']=={'dateTime':'2026-10-20T16:00:00','timeZone':'UTC'}
    assert calls[0][2]['isReminderOn']
    assert [m for m,_,_ in calls]==['POST','GET','PATCH','PATCH','DELETE']


@pytest.mark.parametrize('args',[
 {'action':'create_task','title':'Task','list_id':'other'},
 {'action':'create_task','title':'Task','owner':'bob'},
 {'action':'create_task','title':'Task','remind_at':'2026-10-20T18:00'},
 {'action':'create_task','title':''},
 {'action':'update_task','task_id':'task'},
 {'action':'delete_task','task_id':'..'},
 {'action':'list_tasks','cursor':'https://evil.example/v1.0/me/todo/lists/chosen/tasks'},
 {'action':'list_tasks','cursor':'https://graph.microsoft.com/v1.0/me/todo/lists/other/tasks'},
])
def test_invalid_args_never_reach_provider(args,monkeypatch):
    connected()
    async def fail(*a,**kw):pytest.fail('invalid request must not reach Microsoft')
    monkeypatch.setattr(todo,'_http',fail)
    with pytest.raises(HTTPException):asyncio.run(todo.tasks('alice',args))


def test_unselected_list_and_missing_account_do_not_fall_back(monkeypatch):
    connected();data=todo.record('alice');data.pop('default_list_id');todo.save('alice',data)
    with pytest.raises(HTTPException) as ex:asyncio.run(todo.tasks('alice',{'action':'create_task','title':'Task'}))
    assert ex.value.status_code==422
    with pytest.raises(HTTPException):asyncio.run(todo.lists('bob'))


def test_default_list_is_verified_and_old_selection_cannot_survive_reconnect(monkeypatch):
    connected()
    async def http(method,url,**kw):return response(200,{'id':'selected','displayName':'Samsung list','isOwner':True})
    monkeypatch.setattr(todo,'_http',http)
    assert asyncio.run(todo.select_list('alice','selected'))['default_list_id']=='selected'
    assert not todo.status('bob')['default_list_id']
    asyncio.run(todo.disconnect('alice'))
    assert todo.status('alice')['default_list_id'] is None


def test_task_permission_classification_and_native_registration():
    from src.tool_capabilities import capabilities_for_action,ToolEffect,ResultIntegrity
    from src.agent_tools import FUNCTION_TOOL_SCHEMAS, TOOL_TAGS, function_call_to_tool_block
    block=function_call_to_tool_block("manage_microsoft_todo",'{"action":"list_tasks"}')
    assert block.tool_type=="manage_microsoft_todo"
    assert 'manage_microsoft_todo' in TOOL_TAGS
    schema=next(r['function'] for r in FUNCTION_TOOL_SCHEMAS if r['function']['name']=='manage_microsoft_todo')
    assert 'create_task' in schema['parameters']['properties']['action']['enum']
    for action in ['list_tasks','create_task','delete_task']:
        caps=capabilities_for_action('manage_microsoft_todo',json.dumps({'action':action}))
        assert caps.known and caps.result_integrity==ResultIntegrity.EXTERNAL_UNTRUSTED
        assert (ToolEffect.READ_PRIVATE if action=='list_tasks' else ToolEffect.WRITE_PRIVATE) in caps.effects
        assert (ToolEffect.DESTRUCTIVE in caps.effects)==(action=='delete_task')


def test_api_tokens_cannot_manage_oauth_connection():
    from routes.microsoft_todo_routes import owner
    req=SimpleNamespace(state=SimpleNamespace(api_token=True,api_token_owner='alice',current_user='api'))
    with pytest.raises(HTTPException) as ex:owner(req)
    assert ex.value.status_code==403


def test_tool_requires_owner_and_returns_no_tokens(monkeypatch):
    from src.tools.microsoft_todo import do_manage_microsoft_todo
    assert asyncio.run(do_manage_microsoft_todo('{"action":"list_tasks"}'))['exit_code']==1
    async def fake(owner,args):return {'tasks':[]}
    monkeypatch.setattr(todo,'tasks',fake)
    result=asyncio.run(do_manage_microsoft_todo('{"action":"list_tasks"}',owner='alice'))
    assert result['exit_code']==0 and result['provider']=='microsoft_todo'
    from src.agent_tools import format_tool_result
    assert 'tasks' in format_tool_result('To Do',result)


def test_real_dispatcher_calls_owner_bound_tool(monkeypatch):
    from src.agent_tools import ToolBlock
    from src.tool_execution import execute_tool_block, NO_TOOL_SECURITY_CONTEXT
    import src.tool_execution as execution
    monkeypatch.setattr(execution,'_owner_is_admin',lambda owner:owner=='alice')
    seen=[]
    async def fake(owner,args):seen.append((owner,args));return {'tasks':[]}
    monkeypatch.setattr(todo,'tasks',fake)
    async def run():
        _,result=await execute_tool_block(ToolBlock('manage_microsoft_todo','{"action":"list_tasks"}'),
            owner='alice',security_context=NO_TOOL_SECURITY_CONTEXT)
        assert result['exit_code']==0 and result['provider']=='microsoft_todo'
    asyncio.run(run())
    assert seen==[('alice',{'action':'list_tasks'})]


def test_router_status_redaction_and_strict_body():
    import httpx
    from fastapi import FastAPI
    from routes.microsoft_todo_routes import setup_microsoft_todo_routes
    connected()
    app=FastAPI()
    @app.middleware('http')
    async def login(request,call_next):
        request.state.current_user='alice'
        return await call_next(request)
    app.include_router(setup_microsoft_todo_routes())
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            res=await client.get('/api/microsoft-todo/status')
            assert res.status_code==200 and res.json()['connected']
            assert 'secret' not in res.text and 'tokens' not in res.text
            res=await client.put('/api/microsoft-todo/default-list',json={'list_id':'chosen','owner':'bob'})
            assert res.status_code==422
    asyncio.run(run())


def test_rate_limit_does_not_retry_creating_a_task(monkeypatch):
    connected();calls=[]
    async def http(*args,**kw):calls.append(args);return response(429,{'error':'private upstream detail'})
    monkeypatch.setattr(todo,'_http',http)
    with pytest.raises(HTTPException) as ex:
        asyncio.run(todo.tasks('alice',{'action':'create_task','title':'Task'}))
    assert ex.value.status_code==429 and 'private upstream' not in str(ex.value)
    assert len(calls)==1


def test_pagination_accepts_equivalent_encoding_but_not_another_list():
    assert todo.next_page('https://graph.microsoft.com/v1.0/me/todo/lists/abc=/tasks?$skiptoken=page',
                           '/me/todo/lists/abc%3D/tasks').endswith('?$skiptoken=page')
