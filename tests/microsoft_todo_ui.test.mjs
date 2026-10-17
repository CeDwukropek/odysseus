import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=readFileSync('static/js/settings.js','utf8');
const start=source.indexOf('  async function showMicrosoftTodoForm()');
const code=source.slice(start,source.indexOf('  // ── API form ──',start));
function panelFixture(){
  const elements=new Map(),calls=[];
  const element=()=>({style:{display:''},value:'',children:[],textContent:'',
    replaceChildren(...rows){this.children=rows;},add(row){this.children.push(row);},appendChild(row){this.children.push(row);}});
  const form=element();
  Object.defineProperty(form,'innerHTML',{set(html){for(const [,id] of html.matchAll(/id="([^"]+)"/g))elements.set(id,element());}});
  const state={connected:true,client_id:'app-id',default_list_id:'selected',default_list_name:'Samsung'};
  const fetch=async(url,options={})=>{
    calls.push({url,options});
    const data=url.endsWith('/status')?state:url.endsWith('/lists')?{lists:[{id:'selected',name:'Samsung',is_owner:true}]}:
      url.endsWith('/tasks')?{tasks:[{id:'task',title:'<img src=x onerror=alert(1)>',status:'notStarted'}]}:{ok:true};
    return {ok:true,json:async()=>data};
  };
  const factory=new Function('formEl','el','fetch','renderList','Option','document',code+';return showMicrosoftTodoForm;');
  const show=factory(form,id=>elements.get(id),fetch,async()=>{},function Option(text,value){return {text,value};},{createElement:()=>element()});
  return {show,elements,calls};
}
test('settings restore selected list and save only its ID',async()=>{
  const f=panelFixture();await f.show();
  assert.equal(f.elements.get('uf-todo-list').value,'selected');
  await f.elements.get('uf-todo-save').onclick();
  const save=f.calls.find(c=>c.url.endsWith('/default-list'));
  assert.equal(save.options.method,'PUT');
  assert.deepEqual(JSON.parse(save.options.body),{list_id:'selected'});
});
test('task preview treats external task titles as text',async()=>{
  const f=panelFixture();await f.show();
  await f.elements.get('uf-todo-preview').onclick();
  const child=f.elements.get('uf-todo-tasks').children[0];
  assert.equal(child.textContent,'○ <img src=x onerror=alert(1)>');
  assert.equal(child.children.length,0);
});
