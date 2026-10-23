import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source = readFileSync('static/js/calendar.js','utf8');
const body = source.slice(source.indexOf('async function _syncCaldav('), source.indexOf('function _optimisticEvent('));
function makeSync(payload, urls) {
  const factory = new Function('fetch','API_BASE',body+';return _syncCaldav;');
  return factory(async url => {urls.push(url);return {ok:true,json:async()=>payload};},'');
}
test('automatic open only pulls and explicit sync pushes then pulls', async()=>{
  const urls=[];
  const sync=makeSync({pull:{events:0,calendars:0,errors:[]},push:{events:0,errors:[]}},urls);
  await sync(false);
  await sync(true);
  assert.deepEqual(urls,['/api/calendar/sync?direction=pull','/api/calendar/sync?direction=both']);
});
test('explicit sync reports both write and read failures', async()=>{
  const sync=makeSync({push:{events:2,errors:['write failed']},pull:{events:3,errors:['read failed']}},[]);
  const result=await sync(true);
  assert.equal(result.pushed,2);
  assert.equal(result.events,3);
  assert.deepEqual(result.errors,['write failed','read failed']);
});
