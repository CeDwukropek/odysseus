"""Interactive owner-scoped setup; no credentials returned to the browser."""
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from src.auth_helpers import require_user
from src import microsoft_todo as todo

class StrictBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
class LoginBody(StrictBody):
    client_id:str=Field(min_length=1,max_length=64)
class PollBody(StrictBody):
    poll_id:str=Field(min_length=1,max_length=128)
class ListBody(StrictBody):
    list_id:str=Field(min_length=1,max_length=2048)

def owner(request):
    from src.owner_identity import effective_storage_owner
    resolved=effective_storage_owner(require_user(request))
    if not resolved: raise HTTPException(401,"A verified account owner is required.")
    return resolved

def setup_microsoft_todo_routes():
    router=APIRouter(prefix='/api/microsoft-todo',tags=['Microsoft To Do'])
    @router.get('/status')
    async def status(request:Request): return todo.status(owner(request))
    @router.post('/login/start')
    async def start(request:Request,body:LoginBody): return await todo.start_login(owner(request),body.client_id)
    @router.post('/login/poll')
    async def poll(request:Request,body:PollBody): return await todo.poll_login(owner(request),body.poll_id)
    @router.delete('/connection')
    async def disconnect(request:Request):
        await todo.disconnect(owner(request));return {'ok':True}
    @router.get('/lists')
    async def lists(request:Request): return await todo.lists(owner(request))
    @router.put('/default-list')
    async def selected(request:Request,body:ListBody): return await todo.select_list(owner(request),body.list_id)
    @router.get('/tasks')
    async def tasks(request:Request,cursor:str|None=None):
        return await todo.tasks(owner(request),{'action':'list_tasks',**({'cursor':cursor} if cursor else {})})
    return router
