const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
test('personalizes both bodies, preserves edits and reusable defaults when recipient changes',async()=>{
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id,{value:'',events:{},addEventListener(name,fn){this.events[name]=fn;},insertBefore(){},children:[],replaceChildren(){this.children=[];},append(...items){this.children.push(...items);},checkValidity(){return true;}});
    return elements.get(id);
  };
  const defaults={invitation_title:'Previously saved subject',invitation_body:'Hello {recipient_name},\nPlease sign {contract_name}.\nThank you!',completion_body:'Dear {recipient_name},\nCompleted: {contract_name}.',signing_days:30,completion_days:60};
  let saved;
  const context=vm.createContext({document:{getElementById:get,createElement:tag=>({tag,addEventListener(){}})},window:{},fetch:async(url,options)=>{if(options) saved=JSON.parse(options.body);return {ok:true,json:async()=>defaults};}});
  vm.runInContext(fs.readFileSync('digital_signing/static/email-composer.js','utf8'),context);
  get('recipient-name').value='Jane';
  await context.window.emailComposer.load('Wedding agreement');
  assert.equal(get('invitation-title').value,'');
  get('invitation-title').value='Music for Wedding agreement';get('invitation-title').events.input();
  assert.equal(get('invitation-body').value,'Hello Jane,\nPlease sign Music for Wedding agreement.\nThank you!');
  assert.equal(get('completion-body').value,'Dear Jane,\nCompleted: Wedding agreement.');
  assert.equal(get('invitation-preview').children.find(e=>e.tag==='strong').textContent,'Music for Wedding agreement');
  get('invitation-body').value=get('invitation-body').value.replace('Please sign','Please review and sign');
  get('invitation-body').events.input();
  get('recipient-name').value='Pedro';get('recipient-name').events.input();
  assert.match(get('invitation-body').value,/Hello Pedro/);
  assert.match(get('invitation-body').value,/Please review and sign Music for Wedding agreement/);
  await get('save-email-defaults').events.click();
  assert.equal(saved.invitation_body,'Hello {recipient_name},\nPlease review and sign {invitation_subject}.\nThank you!');
  assert.equal(saved.invitation_title,'Music for Wedding agreement');
  assert.equal(saved.completion_body,defaults.completion_body);
  assert.equal(saved.completion_days,defaults.completion_days);
  // A name changed directly in the body is an intentional custom greeting.
  get('completion-body').value=get('completion-body').value.replace('Pedro','Friends');get('completion-body').events.input();
  get('recipient-name').value='Alex';get('recipient-name').events.input();
  assert.match(get('completion-body').value,/Dear Friends/);
  assert.match(get('invitation-body').value,/Hello Alex/);
});
