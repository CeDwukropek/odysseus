"""Owner-scoped Microsoft To Do client; credentials never enter preferences or tools."""
import asyncio
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit, unquote
import fcntl
import httpx
from fastapi import HTTPException
from core.atomic_io import atomic_write_json
from src.constants import DATA_DIR
from src.secret_storage import encrypt, decrypt
from routes.device_flow import PendingDeviceFlowStore

STORE_FILE = Path(DATA_DIR) / 'microsoft_todo.json'
GRAPH = 'https://graph.microsoft.com/v1.0'
AUTH = 'https://login.microsoftonline.com/common/oauth2/v2.0'
SCOPES = 'https://graph.microsoft.com/Tasks.ReadWrite offline_access'
flows = PendingDeviceFlowStore()
_locks = {}
_active_flows = {}


def owner_lock(owner):
    if owner not in _locks:
        if len(_locks) >= 4096:
            raise HTTPException(503, 'Too many active To Do accounts; restart the service.')
        _locks[owner] = asyncio.Lock()
    return _locks[owner]


@contextmanager
def _store():
    STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(str(STORE_FILE) + '.lock', 'a', encoding='utf-8') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(STORE_FILE.read_text()) if STORE_FILE.exists() else {}
            yield data
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def record(owner):
    with _store() as data:
        return dict(data.get(owner, {}))


def save(owner, value):
    with _store() as data:
        data[owner] = value
        atomic_write_json(STORE_FILE, data, indent=2)
        STORE_FILE.chmod(0o600)


def status(owner):
    data = record(owner)
    return {'connected': bool(data.get('tokens')), 'client_id': data.get('client_id', ''),
            'default_list_id': data.get('default_list_id'), 'default_list_name': data.get('default_list_name', '')}


async def _http(method, url, **kwargs):
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
            return await client.request(method, url, **kwargs)
    except httpx.HTTPError:
        raise HTTPException(503, 'Microsoft is unavailable. Try again later; do not repeat task creation blindly.') from None


def _json(response):
    try:
        result = response.json()
        if not isinstance(result, dict): raise ValueError()
        return result
    except (ValueError, TypeError):
        raise HTTPException(502, 'Microsoft returned an invalid response.') from None


def _token_data(payload, previous=None):
    if not payload.get('access_token'):
        raise HTTPException(502, 'Microsoft did not return an access token.')
    scope = payload.get('scope', '')
    if scope and not any(item.split('/')[-1] == 'Tasks.ReadWrite' for item in scope.split()):
        raise HTTPException(403, 'Microsoft To Do Tasks.ReadWrite permission was not granted.')
    return {'access_token': payload['access_token'],
            'refresh_token': payload.get('refresh_token') or (previous or {}).get('refresh_token'),
            'expires_at': time.time() + max(0, int(payload.get('expires_in', 3600)))}


async def start_login(owner, client_id):
    try: client_id = str(uuid.UUID(client_id))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, 'Provide the Application (client) ID from your Microsoft app registration.') from None
    async with owner_lock(owner):
        response = await _http('POST', AUTH + '/devicecode', data={'client_id':client_id,'scope':SCOPES})
        payload = _json(response)
        if response.status_code != 200:
            raise HTTPException(422, 'Microsoft rejected the application. Check Client ID, supported account types and Allow public client flows.')
        if not all(payload.get(k) for k in ('device_code','user_code','verification_uri')):
            raise HTTPException(502, 'Microsoft returned an incomplete sign-in response.')
        interval = max(5, int(payload.get('interval', 5)))
        expires = min(1800, int(payload.get('expires_in', 900)))
        current = record(owner)
        if not current.get('generation'):
            current['generation'] = uuid.uuid4().hex
            save(owner, current)
        previous_flow=_active_flows.pop(owner,None)
        if previous_flow: flows.pop(previous_flow)
        poll_id = flows.add({'owner':owner,'generation':current['generation'],
                             'client_id':client_id,'device_code':payload['device_code']},
                            interval=interval, expires_in=expires)
        _active_flows[owner]=poll_id
        # Microsoft's response must not inject an arbitrary browser sign-in URL.
        return {'poll_id':poll_id,'user_code':payload['user_code'],
                'verification_uri':'https://microsoft.com/devicelogin',
                'interval':interval,'expires_in':expires}


async def poll_login(owner, poll_id):
    async with owner_lock(owner):
        flow = flows.get_payload(poll_id)
        if not flow or flow.get('owner') != owner:
            raise HTTPException(404, 'Sign-in session not found or expired. Start again.')
        if record(owner).get('generation') != flow['generation']:
            flows.pop(poll_id)
            raise HTTPException(409, 'Connection changed. Start sign-in again.')
        if flows.is_throttled(poll_id): return {'status':'pending'}
        flows.schedule_next(poll_id)
        response = await _http('POST', AUTH + '/token', data={
            'client_id':flow['client_id'],'device_code':flow['device_code'],
            'grant_type':'urn:ietf:params:oauth:grant-type:device_code'})
        payload = _json(response)
        if response.status_code != 200:
            error = payload.get('error')
            if error == 'authorization_pending': return {'status':'pending'}
            if error == 'slow_down':
                flows.slow_down(poll_id)
                return {'status':'pending','interval':10}
            flows.pop(poll_id)
            raise HTTPException(401, 'Microsoft sign-in was declined or expired. Start again.')
        tokens = _token_data(payload)
        if not tokens['refresh_token']:
            raise HTTPException(422, 'Offline access was not granted; reconnect with offline_access permission.')
        save(owner, {'generation':uuid.uuid4().hex,'client_id':flow['client_id'],
                     'tokens':encrypt(json.dumps(tokens))})
        flows.pop(poll_id)
        return {'status':'connected'}


async def disconnect(owner):
    async with owner_lock(owner):
        previous_flow=_active_flows.pop(owner,None)
        if previous_flow: flows.pop(previous_flow)
        save(owner, {'generation':uuid.uuid4().hex})


async def access_token(owner, *, force=False):
    # Caller holds owner_lock, including refresh rotation and disconnect.
    data = record(owner)
    if not data.get('tokens'): raise HTTPException(401, 'Connect Microsoft To Do in Settings → Integrations first.')
    try: tokens = json.loads(decrypt(data['tokens']))
    except Exception: raise HTTPException(401, 'Reconnect Microsoft To Do.') from None
    if not force and tokens.get('expires_at', 0) > time.time() + 60:
        return tokens['access_token']
    if not tokens.get('refresh_token'): raise HTTPException(401, 'Reconnect Microsoft To Do.')
    response = await _http('POST', AUTH + '/token', data={
        'client_id':data['client_id'],'grant_type':'refresh_token',
        'refresh_token':tokens['refresh_token'],'scope':SCOPES})
    if response.status_code != 200:
        raise HTTPException(401, 'Microsoft session expired or permission was revoked. Reconnect To Do.')
    updated = _token_data(_json(response), tokens)
    data['tokens'] = encrypt(json.dumps(updated)); save(owner, data)
    return updated['access_token']


async def graph(owner, method, path, *, body=None):
    token = await access_token(owner)
    kwargs = {'headers':{'Authorization':'Bearer '+token}}
    if body is not None: kwargs['json'] = body
    response = await _http(method, GRAPH + path, **kwargs)
    if response.status_code == 401:
        # A 401 is a rejected operation; retry once after rotating credentials.
        kwargs['headers']['Authorization'] = 'Bearer ' + await access_token(owner, force=True)
        response = await _http(method, GRAPH + path, **kwargs)
    if response.status_code >= 400 or response.status_code < 200 or response.status_code >= 300:
        code = response.status_code
        message = {401:'Reconnect Microsoft To Do.',403:'Microsoft denied access to this task/list.',
                   404:'Task or list no longer exists. Refresh the list.',
                   429:'Microsoft rate limit reached. Wait before retrying.'}.get(code,
                   'Microsoft did not confirm the operation. Check the list before retrying creation.')
        raise HTTPException(code if code in (401,403,404,429) else 502, message)
    return {} if response.status_code == 204 else _json(response)


def identifier(value):
    if not isinstance(value, str) or not value or len(value)>2048 or value in ('.','..') or any(ord(c)<32 for c in value):
        raise HTTPException(422, 'Invalid task/list ID.')
    return quote(value, safe='')


async def lists(owner):
    async with owner_lock(owner):
        rows, path = [], '/me/todo/lists?$top=100'
        for _ in range(50):
            result = await graph(owner, 'GET', path)
            rows.extend({'id':r['id'],'name':r.get('displayName',''),
                         'is_owner':r.get('isOwner',True),'is_shared':r.get('isShared',False)} for r in result.get('value',[]))
            link = result.get('@odata.nextLink')
            if not link: return {'lists':rows}
            path = next_page(link, '/me/todo/lists')
        raise HTTPException(503, 'Too many lists to return a complete catalogue.')


def next_page(link, resource):
    url = urlsplit(link)
    if url.scheme!='https' or url.netloc!='graph.microsoft.com' or unquote(url.path)!=unquote('/v1.0'+resource) or url.fragment:
        raise HTTPException(422, 'Invalid Microsoft pagination link.')
    return resource + ('?'+url.query if url.query else '')


async def select_list(owner, list_id):
    async with owner_lock(owner):
        row = await graph(owner, 'GET', '/me/todo/lists/'+identifier(list_id))
        if row.get('isOwner') is False:
            raise HTTPException(403, 'Select a list owned by your Microsoft account.')
        data = record(owner)
        data.update(default_list_id=list_id, default_list_name=row.get('displayName',''))
        save(owner, data)
        return status(owner)


def task_fields(args, *, creating):
    result = {}
    if creating or 'title' in args:
        title = args.get('title')
        if not isinstance(title,str) or not title.strip() or len(title)>500:
            raise HTTPException(422, 'Provide a task title (1–500 characters).')
        result['title'] = title.strip()
    if 'notes' in args:
        notes=args['notes']
        if not isinstance(notes,str) or len(notes)>10000: raise HTTPException(422,'Notes are too long.')
        result['body']={'contentType':'text','content':notes}
    from datetime import datetime, timezone
    for key,field in [('due_at','dueDateTime'),('remind_at','reminderDateTime')]:
        if key not in args: continue
        raw=args[key]
        if raw is None:
            result[field]=None
            if key=='remind_at': result['isReminderOn']=False
            continue
        try:
            dt=datetime.fromisoformat(raw.replace('Z','+00:00'))
            if dt.tzinfo is None or dt.utcoffset() is None: raise ValueError()
        except (ValueError,TypeError,AttributeError):
            raise HTTPException(422,'Use an ISO datetime with timezone offset, e.g. 2026-10-20T18:00:00+02:00.') from None
        result[field]={'dateTime':dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat(),'timeZone':'UTC'}
        if key=='remind_at': result['isReminderOn']=True
    return result


def task_view(row):
    return {key:row.get(key) for key in ('id','title','status','dueDateTime','isReminderOn','reminderDateTime','body')}


async def tasks(owner, args):
    action=args.get('action','list_tasks')
    fields={'list_tasks':{'action','cursor'},'create_task':{'action','title','notes','due_at','remind_at'},
            'update_task':{'action','task_id','title','notes','due_at','remind_at'},
            'complete_task':{'action','task_id'},'delete_task':{'action','task_id'}}
    if action not in fields or set(args)-fields[action]: raise HTTPException(422,'Unknown action or arguments.')
    # Validate before any provider call.
    payload=task_fields(args,creating=action=='create_task') if action in ('create_task','update_task') else None
    if action=='update_task' and not payload: raise HTTPException(422,'Provide at least one changed field.')
    task_id=identifier(args.get('task_id')) if action in ('update_task','complete_task','delete_task') else None
    async with owner_lock(owner):
        data=record(owner); selected=data.get('default_list_id')
        if not selected: raise HTTPException(422,'Choose the Samsung-synced list in Settings → Integrations → Microsoft To Do.')
        resource='/me/todo/lists/'+identifier(selected)+'/tasks'
        if action=='list_tasks':
            path=next_page(args['cursor'],resource) if args.get('cursor') else resource+'?$top=50'
            result=await graph(owner,'GET',path)
            return {'tasks':[task_view(r) for r in result.get('value',[])],
                    'next_cursor':result.get('@odata.nextLink'),'list_name':data.get('default_list_name','')}
        if action=='create_task': result=await graph(owner,'POST',resource,body=payload)
        elif action=='delete_task':
            await graph(owner,'DELETE',resource+'/'+task_id)
            return {'deleted':True,'task_id':args['task_id']}
        else:
            if action=='complete_task': payload={'status':'completed'}
            result=await graph(owner,'PATCH',resource+'/'+task_id,body=payload)
        return {'task':task_view(result),'list_name':data.get('default_list_name','')}
