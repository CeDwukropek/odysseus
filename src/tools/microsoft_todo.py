"""Microsoft-backed reminders, distinct from internal tasks and calendar events."""
import json
from fastapi import HTTPException
from src.tools._common import _parse_tool_args
from src import microsoft_todo as todo

async def do_manage_microsoft_todo(content,owner=None):
    from src.owner_identity import effective_storage_owner
    owner=effective_storage_owner(owner)
    if not owner:
        return {'error':'A verified account owner is required.','exit_code':1}
    try:
        args=_parse_tool_args(content)
        if not isinstance(args,dict): raise ValueError()
        result=await todo.tasks(owner,args)
        return {'response':json.dumps(result,ensure_ascii=False),'exit_code':0,'provider':'microsoft_todo',
                'notice':'Saved/read in Microsoft To Do. Delivery to Samsung Reminder depends on phone synchronization.'}
    except HTTPException as error:
        return {'error':error.detail,'status':error.status_code,'exit_code':1}
    except (ValueError,TypeError):
        return {'error':'Invalid Microsoft To Do arguments.','exit_code':1}
