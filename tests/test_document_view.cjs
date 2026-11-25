// Run with: node --test tests/test_document_view.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
class Element {
  constructor() {
    this.children = []; this.events = {}; this.value = ''; this.hidden = true;
    this.style = {setProperty() {}}; this.classList = {add() {}, toggle() {}, contains(){return false;}};
    this.attributes = {}; this.dataset={}; this.clientWidth = 900;
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  addEventListener(name, fn) { this.events[name] = fn; }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  scrollIntoView() {}
  focus() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
  reportValidity() { return true; }
  getBoundingClientRect() { return {left:0, right:300, top:0, bottom:50, width:300, height:50}; }
}
async function viewer({vendor=true, state='awaiting_vendor', superseded=false, placed=true, recipient=false, review=false, sent=false, prefills={}, correctionNote=null, defaults={}, labels={}, extraFields=[], lockedFields=[], closeBlocked=false, validationErrors=null, submissionError=false, deliveryError=null, sendStatus='local', sendError=false}={}) {
  const elements = new Map();
  const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  get('zoom').value = 'fit';
  const fields = [
    {key:'client.signing_date',type:'date',role:'client',label:'Signing date',required:true},
    {key:'client.start',type:'time',role:'client',label:'Start time',required:true},
    {key:'client.date',type:'date',role:'client',label:'Event date',required:true},
    {key:'client.name',type:'text',role:'client',label:'Name',required:true},
    {key:'client.email',type:'text',role:'client',label:'Email',required:true},
    {key:'client.provided_name',type:'text',role:'client',label:'Provided name',required:true},
    {key:'client.provided_email',type:'text',role:'client',label:'Provided email',required:true},
    {key:'client.signature',type:'signature',role:'client',label:'Client signature',required:true},
  ];
  fields.push(...extraFields);
  const vendorField = {key:'vendor.signature',type:'signature',role:'vendor',label:'Vendor signature',required:true};
  if (placed) fields.push(vendorField);
  for (const field of fields) {
    if (Object.hasOwn(defaults,field.key)) field.default=defaults[field.key];
    if (Object.hasOwn(labels,field.key)) field.label=labels[field.key];
    if (lockedFields.includes(field.key)) field.locked=true;
  }
  const placement = key => ({key,box:[10,10,100,20],font_size:12,masks:[]});
  const snapshot = {name:'Test contract',version:1,fields,prefill_values:prefills,correction_note:correctionNote,created_at:'2026-11-17',warnings:[],pages:[
    {width:600,height:800,placements:fields.map(f=>placement(f.key)),text:'Page one',image_url:'/1.png'},
    {width:600,height:800,placements:[],text:'Page two',image_url:'/2.png'},
  ]};
  const workflow = {state,superseded,sent,signing_date:'2026-11-17',vendor_fields:[vendorField],client_consent:'Client consent',vendor_consent:'Vendor consent',
    client: state === 'editable' ? null : {values:{'client.name':'Jane Smith','client.signature':{image:'data:image/png;base64,client'}}}};
  const calls=[];
  const context=vm.createContext({
    document:{getElementById:get,createElement:()=>new Element(),body:new Element(),addEventListener() {}},
    location:{pathname:recipient?'/sign/private-token':vendor?'/countersign/test':'/preview/test',search:recipient||review?'?review=1':''},
    window:{SignatureEditor:class {open() {}},addEventListener() {},closed:false,close(){this.closeCalls=(this.closeCalls||0)+1;if(!closeBlocked)this.closed=true;}},
    setTimeout(fn){fn();},
    URLSearchParams, Option:class { constructor(text,value) {this.text=text;this.value=value;} }, ResizeObserver:class {observe() {}},setInterval() {},
    fetch:async (url, options) => {
      calls.push({url,options});
      let data = url.endsWith('/workflow') ? workflow : (url.endsWith('/renders') || url === '/api/sign/private-token') ? [{version:1}] : snapshot;
      if (options?.method === 'POST') data = {...workflow,state:'completed',vendor:{values:JSON.parse(options.body).values},final_pdf_url:'/final.pdf'};
      if (url.endsWith('/validate')) data = {valid:!validationErrors,errors:validationErrors || {}};
      if (url.endsWith('/submit')) data = {...workflow,state:'awaiting_vendor',client:{values:JSON.parse(options.body).values}};
      if (url.endsWith('/submit') || url.endsWith('/countersign')) {
        if (submissionError) return {ok:false,json:async()=>({detail:'Unable to save the signature.'})};
        if (deliveryError) data.email_delivery={error:deliveryError};
      }
      if (url.endsWith('/send')) {
        if (sendError) return {ok:false,json:async()=>({detail:'Unable to create the invitation.'})};
        data = {link:'/sign/new-private-token',delivery_status:sendStatus,delivery_error:deliveryError};
      }
      if (url === '/api/email-delivery') data = {mode:'local'};
      if (url.endsWith('/request-corrections')) data = {version:2,email:{link:'/sign/corrected',delivery_status:'local'}};
      return {ok:true,json:async()=>data};
    },
  });
  vm.runInContext(fs.readFileSync('digital_signing/static/preview.js','utf8'),context);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(get('preview-notice').textContent,undefined);
  return {context,get,calls,run:code=>vm.runInContext(code,context)};
}
test('vendor sees saved client answers and can sign only vendor fields on the rendered pages',async()=>{
  const v=await viewer();
  assert.equal(v.run("controls.find(c=>c.key==='client.name').input.value"),'Jane Smith');
  assert.equal(v.run("controls.find(c=>c.key==='client.name').input.disabled"),true);
  assert.equal(v.run("controls.find(c=>c.key==='client.signature').input.children[0].src"),'data:image/png;base64,client');
  assert.equal(v.run("controls.find(c=>c.key==='vendor.signature').input.disabled"),false);
  assert.equal(v.get('submit-confirmation').hidden,true);
  v.run('showPage(1, false)');
  assert.equal(v.get('page-label').textContent,'Page 2 of 2');
  assert.equal(v.get('submit-confirmation').hidden,false);
  v.get('signer-name').value='Vendor'; v.get('signing-consent').checked=true;
  v.run("values['vendor.signature']={method:'drawn',image:'vendor'}");
  await v.get('preview-form').events.submit({preventDefault(){}});
  const submitted=v.calls.find(c=>c.options?.method === 'POST');
  assert.match(submitted.url,/\/countersign$/);
  assert.deepEqual(Object.keys(JSON.parse(submitted.options.body).values),['vendor.signature']);
  assert.equal(v.run('controls.every(c=>c.input.disabled)'),true);
  assert.equal(v.get('receipt-download').href,'/final.pdf');
  assert.equal(v.get('signing-success-dialog').open,true);
  assert.equal(v.get('signing-success-title').textContent,'The agreement has been completed.');
  assert.equal(v.get('signing-success-description').textContent,'You may close this window now.');
});

test('client submission opens an accessible confirmation and dismissal does not resubmit',async()=>{
  const v=await viewer({vendor:false,recipient:true,state:'editable'});
  assert.notEqual(v.get('signing-success-dialog').open,true);
  v.run('showPage(1,false)'); v.get('signing-consent').checked=true;
  await v.get('preview-form').events.submit({preventDefault(){}});
  assert.equal(v.get('signing-success-dialog').open,true);
  assert.equal(v.get('signing-success-title').textContent,'Your agreement has been submitted.');
  assert.equal(v.get('signing-success-description').textContent,'It is awaiting the vendor’s signature. You may close this window now.');
  assert.match(v.get('receipt-description').textContent,/You may close this window now\./);
  assert.equal(v.get('signing-success-warning').hidden,true);
  assert.equal(v.get('receipt-download').hidden,true);
  v.get('signing-success-close').events.click();
  v.run('applyWorkflow(workflow)');
  assert.equal(v.context.window.closed,true);
  assert.equal(v.context.window.closeCalls,1);
  assert.equal(v.calls.filter(c=>c.url.endsWith('/submit')).length,1);
});

test('failed validation, failed saves, and preparation checks never show signing success',async()=>{
  for (const options of [{validationErrors:{'client.name':'Enter a name.'}},{submissionError:true},{review:true}]) {
    const v=await viewer({vendor:false,state:'editable',...options});
    v.run('showPage(1,false)'); v.get('signing-consent').checked=true;
    await v.get('preview-form').events.submit({preventDefault(){}});
    assert.notEqual(v.get('signing-success-dialog').open,true);
  }
  const vendor=await viewer({submissionError:true});
  vendor.run('showPage(1,false)'); vendor.get('signing-consent').checked=true;
  await vendor.get('preview-form').events.submit({preventDefault(){}});
  assert.notEqual(vendor.get('signing-success-dialog').open,true);
});

test('saved signature confirmation keeps delivery warnings visible',async()=>{
  const v=await viewer({deliveryError:'Gmail is unavailable.'});
  v.run('showPage(1,false)'); v.get('signing-consent').checked=true;
  await v.get('preview-form').events.submit({preventDefault(){}});
  assert.equal(v.get('signing-success-dialog').open,true);
  assert.equal(v.get('signing-success-warning').hidden,false);
  assert.match(v.get('signing-success-warning').textContent,/Gmail is unavailable/);
});

test('reopening a signed document does not trigger a new success modal',async()=>{
  for (const options of [{vendor:false,state:'awaiting_vendor'},{vendor:true,state:'completed'}]) {
    const v=await viewer(options);
    assert.notEqual(v.get('signing-success-dialog').open,true);
  }
});
test('missing vendor placeholder gets an editable completion-record field on the last page',async()=>{
  const v=await viewer({placed:false});
  assert.equal(v.get('additional-vendor-fields').hidden,true);
  v.run('showPage(1, false)');
  assert.equal(v.get('additional-vendor-fields').hidden,false);
  assert.equal(v.run("controls.find(c=>c.key==='vendor.signature').input.disabled"),false);
});
test('vendor cannot edit superseded, completed, or unsubmitted versions',async()=>{
  for(const options of [{superseded:true},{state:'completed'},{state:'editable'}]) {
    const v=await viewer(options);
    assert.equal(v.run('controls.every(c=>c.input.disabled)'),true);
    v.run('showPage(1,false)');
    assert.equal(v.get('validate').hidden,true);
  }
});
test('client view still permits only client fields before submission',async()=>{
  const v=await viewer({vendor:false,state:'editable'});
  assert.equal(v.run("controls.find(c=>c.key==='client.name').input.disabled"),false);
  assert.equal(v.run("controls.find(c=>c.key==='vendor.signature').input.disabled"),true);
  assert.equal(v.get('validate').textContent,'Submit agreement');
});

test('recipient link uses only scoped endpoints and cannot enable preparation controls',async()=>{
  const v=await viewer({vendor:false,recipient:true,state:'editable'});
  assert.equal(v.get('review-details').hidden,true);
  assert.equal(v.get('validate').textContent,'Submit agreement');
  assert.equal(v.run("controls.find(c=>c.key==='vendor.signature').input.disabled"),true);
  assert.ok(v.calls.every(c=>c.url.startsWith('/api/sign/private-token')));
});

test('review page sends its exact version without navigating away and then locks', async()=>{
  const v=await viewer({vendor:false,review:true,state:'editable'});
  assert.equal(v.get('review-send').hidden,false);
  await v.get('review-send').events.click();
  assert.equal(v.get('send-dialog').open,true);
  v.get('recipient-name').value='Jane'; v.get('recipient-email').value='jane@example.com';
  v.get('send-reviewed').checked=true;
  await v.get('send-form').events.submit({preventDefault(){}});
  const call=v.calls.find(c=>c.url.endsWith('/send'));
  assert.equal(call.url,'/api/contracts/test/renders/1/send');
  assert.equal(JSON.parse(call.options.body).recipient_email,'jane@example.com');
  assert.equal(v.get('send-dialog').open,false);
  assert.equal(v.get('sent-client-link').href,'/sign/new-private-token');
  assert.equal(v.get('review-send').hidden,true);
  assert.equal(v.run('controls.every(c=>c.input.disabled)'),true);
});
test('sent reviews are locked but recipient can still complete the sent contract',async()=>{
  const review=await viewer({vendor:false,review:true,state:'editable',sent:true});
  assert.equal(review.get('review-send').hidden,true);
  assert.equal(review.run('controls.every(c=>c.input.disabled)'),true);
  const recipient=await viewer({vendor:false,recipient:true,state:'editable',sent:true});
  assert.equal(recipient.run("controls.find(c=>c.key==='client.name').input.disabled"),false);
  assert.equal(recipient.get('review-send').hidden,true);
});

test('successful Gmail send shows a dismissible confirmation without sending again',async()=>{
  const v=await viewer({vendor:false,review:true,state:'editable',sendStatus:'sent'});
  await v.get('review-send').events.click();
  await v.get('send-form').events.submit({preventDefault(){}});
  assert.equal(v.get('send-dialog').open,false);
  assert.equal(v.get('signing-success-dialog').open,true);
  assert.equal(v.get('signing-success-dialog').dataset.tone,'success');
  assert.equal(v.get('signing-success-title').textContent,'The agreement has been sent to the client.');
  assert.equal(v.get('signing-success-description').textContent,'You’ll be notified when the client signs. You may close this window now.');
  v.run('applyWorkflow(workflow)');
  assert.equal(v.get('signing-success-title').textContent,'The agreement has been sent to the client.');
  v.get('signing-success-close').events.click();
  assert.equal(v.context.window.closed,true);
  assert.equal(v.context.window.closeCalls,1);
  assert.equal(v.calls.filter(c=>c.url.endsWith('/send')).length,1);
});

test('local test email confirmation explicitly says no email was sent',async()=>{
  const v=await viewer({vendor:false,review:true,state:'editable',sendStatus:'local'});
  await v.get('review-send').events.click();
  await v.get('send-form').events.submit({preventDefault(){}});
  assert.equal(v.get('signing-success-dialog').open,true);
  assert.equal(v.get('signing-success-dialog').dataset.tone,'info');
  assert.match(v.get('signing-success-description').textContent,/No email was sent/);
});

test('unconfirmed email delivery gets a warning popup, never a sent confirmation',async()=>{
  for (const sendStatus of ['failed','unknown','pending',null]) {
    const v=await viewer({vendor:false,review:true,state:'editable',sendStatus,deliveryError:'Check Gmail Sent before retrying.'});
    await v.get('review-send').events.click();
    await v.get('send-form').events.submit({preventDefault(){}});
    assert.equal(v.get('signing-success-dialog').open,true);
    assert.equal(v.get('signing-success-dialog').dataset.tone,'warning');
    assert.equal(v.get('signing-success-title').textContent,'Email delivery needs attention.');
    assert.match(v.get('signing-success-description').textContent,/delivery has not been confirmed/);
    assert.match(v.get('signing-success-warning').textContent,/Check Gmail Sent/);
    assert.equal(v.get('signing-success-warning').hidden,false);
    assert.equal(v.get('review-send').hidden,true);
  }
});

test('invitation creation failure keeps the send form open without a confirmation',async()=>{
  const v=await viewer({vendor:false,review:true,state:'editable',sendError:true});
  await v.get('review-send').events.click();
  await v.get('send-form').events.submit({preventDefault(){}});
  assert.equal(v.get('send-dialog').open,true);
  assert.notEqual(v.get('signing-success-dialog').open,true);
  assert.match(v.get('send-error').textContent,/Unable to create/);
  assert.equal(v.get('send-confirm').disabled,false);
});

test('sending prefills exact recipient tags before personalizing the email', async()=>{
  const v=await viewer({vendor:false,review:true,state:'editable',defaults:{'client.provided_name':'  Jane Smith  ','client.provided_email':'  jane@example.com  '}});
  v.run("window.emailComposer={load:async()=>{globalThis.composedName=document.getElementById('recipient-name').value;},values:()=>({})}");
  await v.get('review-send').events.click();
  assert.equal(v.get('recipient-name').value,'Jane Smith');
  assert.equal(v.get('recipient-email').value,'jane@example.com');
  assert.equal(v.run('composedName'),'Jane Smith');
  await v.get('send-form').events.submit({preventDefault(){}});
  const payload=JSON.parse(v.calls.find(c=>c.url.endsWith('/send')).options.body);
  assert.equal(payload.recipient_name,'Jane Smith');
  assert.equal(payload.recipient_email,'jane@example.com');
});

test('missing recipient tags, defaults, and REQUIRED placeholders leave sending inputs empty', async()=>{
  for (const defaults of [{},{'client.provided_name':'REQUIRED','client.provided_email':' required '},{'client.provided_name':'  ','client.provided_email':'not-an-email'}]) {
    const v=await viewer({vendor:false,review:true,state:'editable',defaults});
    await v.get('review-send').events.click();
    assert.equal(v.get('recipient-name').value,'');
    assert.equal(v.get('recipient-email').value,'');
  }
  const v=await viewer({vendor:false,review:true,state:'editable',defaults:{'client.provided_name':'Jane','client.provided_email':'jane@example.com'}});
  v.run("snapshot.fields=snapshot.fields.filter(f=>!['client.provided_name','client.provided_email'].includes(f.key))");
  await v.get('review-send').events.click();
  assert.equal(v.get('recipient-name').value,'');
  assert.equal(v.get('recipient-email').value,'');
});

test('recipient fields prefill independently and ignore temporary review answers', async()=>{
  const v=await viewer({vendor:false,review:true,state:'editable',defaults:{'client.provided_name':'Jane'}});
  v.run("updateValue(definition('client.provided_name'),'Temporary Name'); updateValue(definition('client.provided_email'),'temporary@example.com')");
  await v.get('review-send').events.click();
  assert.equal(v.get('recipient-name').value,'Jane');
  assert.equal(v.get('recipient-email').value,'');
  const emailOnly=await viewer({vendor:false,review:true,state:'editable',defaults:{'client.provided_email':'jane@example.com'}});
  await emailOnly.get('review-send').events.click();
  assert.equal(emailOnly.get('recipient-name').value,'');
  assert.equal(emailOnly.get('recipient-email').value,'jane@example.com');
});

test('recipient edits including cleared values survive closing and reopening the sending dialog', async()=>{
  const v=await viewer({vendor:false,review:true,state:'editable',defaults:{'client.provided_name':'Jane','client.provided_email':'jane@example.com'}});
  await v.get('review-send').events.click();
  v.get('recipient-name').value=''; v.get('recipient-email').value='assistant@example.com';
  v.get('send-cancel').events.click();
  await v.get('review-send').events.click();
  assert.equal(v.get('recipient-name').value,'');
  assert.equal(v.get('recipient-email').value,'assistant@example.com');
  v.get('recipient-name').value='Assistant';
  await v.get('send-form').events.submit({preventDefault(){}});
  const payload=JSON.parse(v.calls.find(c=>c.url.endsWith('/send')).options.body);
  assert.equal(payload.recipient_name,'Assistant');
  assert.equal(payload.recipient_email,'assistant@example.com');
});

test('sending never falls back to client name or email tags', async()=>{
  const defaults={'client.name':'Signature name','client.email':'signature@example.com'};
  const v=await viewer({vendor:false,review:true,state:'editable',defaults});
  v.run("snapshot.fields=snapshot.fields.filter(f=>!['client.provided_name','client.provided_email'].includes(f.key))");
  await v.get('review-send').events.click();
  assert.equal(v.get('recipient-name').value,'');
  assert.equal(v.get('recipient-email').value,'');
});

test('provided recipient details do not prefill the name the client enters when signing', async()=>{
  const defaults={'client.provided_name':'Jane Smith','client.provided_email':'jane@example.com'};
  const review=await viewer({vendor:false,review:true,state:'editable',defaults});
  await review.get('review-send').events.click();
  assert.equal(review.get('recipient-name').value,'Jane Smith');
  assert.equal(review.get('recipient-email').value,'jane@example.com');
  assert.equal(review.run("controls.find(c=>c.key==='client.name').input.value"),'');
  const recipient=await viewer({vendor:false,recipient:true,state:'editable',sent:true,defaults});
  assert.equal(recipient.run("controls.find(c=>c.key==='client.name').input.value"),'');
  assert.equal(recipient.run("controls.find(c=>c.key==='client.name').input.disabled"),false);
  assert.equal(recipient.run("values['client.provided_name']"),'Jane Smith');
  recipient.run("updateValue(definition('client.name'),'Jane A. Smith')");
  assert.equal(recipient.run("values['client.name']"),'Jane A. Smith');
  assert.equal(recipient.run("values['client.provided_name']"),'Jane Smith');
});

test('document dates use MM-DD-YYYY while stored values stay ISO', async()=>{
  const v=await viewer({vendor:false,state:'editable'});
  v.run("updateValue(definition('client.date'), '2027-06-12')");
  assert.equal(v.run("controls.find(c=>c.key==='client.date').input.textContent"),'06-12-2027 (Saturday)');
  assert.equal(v.run("values['client.date']"),'2027-06-12');
  v.run("updateValue(definition('client.date'), '')");
  assert.equal(v.run("controls.find(c=>c.key==='client.date').input.textContent"),'MM-DD-YYYY');
});

test('signing dates are prefilled and locked; times offer quarter-hour AM/PM choices',async()=>{
  const v=await viewer({vendor:false,state:'editable'});
  assert.equal(v.run("controls.find(c=>c.key==='client.signing_date').input.textContent"),'11-17-2026 (Tuesday)');
  assert.equal(v.run("controls.find(c=>c.key==='client.signing_date').input.disabled"),true);
  assert.equal(v.run("controls.find(c=>c.key==='client.date').input.disabled"),false);
  v.run("globalThis.timeInput = makeInput(definition('client.start'))");
  assert.equal(v.run('timeInput.children[0].children[0].children.length'),13);
  assert.equal(v.run('timeInput.children[1].children[0].children.length'),5);
  assert.equal(v.run('timeInput.children[2].children[0].children.length'),3);
  v.run("timeInput.value='12:15'");
  assert.equal(v.run('timeInput.children[0].children[0].value'),'12');
  assert.equal(v.run('timeInput.children[1].children[0].value'),'15');
  assert.equal(v.run('timeInput.children[2].children[0].value'),'PM');
  assert.equal(v.run('timeInput.value'),'12:15');
});

test('Other event editor preserves custom values and time components convert correctly', async()=>{
  const v=await viewer({vendor:false,state:'editable'});
  v.run("globalThis.eventField={key:'client.event_type',type:'select',role:'client',label:'Event type',options:['Wedding','Corporate']};globalThis.eventEditor=makeInput(eventField)");
  assert.equal(v.run('eventEditor.children[0].children[0].children[3].text'),'Other');
  v.run("eventEditor.value='Other: Graduation party'");
  assert.equal(v.run('eventEditor.children[1].children[0].value'),'Graduation party');
  assert.equal(v.run('eventEditor.value'),'Other: Graduation party');
  v.run("globalThis.timeEditor=makeInput(definition('client.start'));timeEditor.value='00:15'");
  assert.equal(v.run('timeEditor.children[0].children[0].value'),'12');
  assert.equal(v.run('timeEditor.children[2].children[0].value'),'AM');
  assert.equal(v.run('timeEditor.value'),'00:15');
  v.run("timeEditor.value='23:45'");
  assert.equal(v.run('timeEditor.children[0].children[0].value'),'11');
  assert.equal(v.run('timeEditor.children[2].children[0].value'),'PM');
  assert.equal(v.run('timeEditor.value'),'23:45');
});

test('vendor requests corrections from the review page and the old version locks',async()=>{
  const v=await viewer();
  assert.equal(v.get('request-corrections').hidden,false);
  await v.get('request-corrections').events.click();
  assert.equal(v.get('corrections-dialog').open,true);
  assert.equal(v.get('corrections-confirm').textContent,'Create test request');
  v.get('corrections-note').value='Please correct the venue address.';
  await v.get('corrections-form').events.submit({preventDefault(){}});
  const request=v.calls.find(c=>c.url.endsWith('/request-corrections'));
  assert.deepEqual(JSON.parse(request.options.body),{note:'Please correct the venue address.'});
  assert.equal(v.get('corrections-dialog').open,false);
  assert.equal(v.get('request-corrections').hidden,true);
  assert.equal(v.run('locked'),true);
  assert.equal(v.get('sent-client-link').href,'/sign/corrected');
  assert.equal(v.get('send-result-title').textContent,'Test correction request created');
});
test('client correction view restores answers, shows note, and requires a fresh signature and date',async()=>{
  const v=await viewer({vendor:false,recipient:true,state:'editable',sent:true,
    prefills:{'client.name':'Jane Smith','client.date':'2027-06-12','client.start':'14:30','client.signing_date':'1900-01-01','client.signature':{image:'old'}},
    correctionNote:'Please correct the date. <script> is ordinary text.'});
  assert.equal(v.get('request-corrections').hidden,true);
  assert.equal(v.get('correction-note-panel').hidden,false);
  assert.equal(v.get('correction-note-text').textContent,'Please correct the date. <script> is ordinary text.');
  assert.equal(v.run("values['client.name']"),'Jane Smith');
  assert.equal(v.run("values['client.date']"),'2027-06-12');
  assert.equal(v.run("values['client.start']"),'14:30');
  assert.equal(v.run("values['client.signing_date']"),'2026-11-17');
  assert.equal(v.run("values['client.signature']"),undefined);
  assert.equal(v.run("controls.find(c=>c.key==='client.name').input.disabled"),false);
  assert.notEqual(v.get('signing-consent').checked,true);
});
test('correction action is unavailable on completed, superseded, or unsubmitted versions',async()=>{
  for(const options of [{state:'completed'},{superseded:true},{state:'editable'}]) {
    const v=await viewer(options);
    assert.equal(v.get('request-corrections').hidden,true);
    await v.get('request-corrections').events.click();
    assert.notEqual(v.get('corrections-dialog').open,true);
  }
});

test('explicit dropdown other=false uses only listed choices even for event types',async()=>{
  const v=await viewer({vendor:false,state:'editable'});
  v.run("globalThis.closed={key:'client.event_type',type:'select',role:'client',label:'Type of event',options:['Wedding','Corporate'],allow_other:false};globalThis.closedInput=makeInput(closed)");
  assert.equal(v.run('allowsOther(closed)'),false);
  assert.equal(v.run('closedInput.children.map(c=>c.text).join(",")'),'Choose…,Wedding,Corporate');
  assert.equal(v.run('documentButton(closed)'),false);
  v.run("closedInput.value='Wedding';closedInput.events.input()");
  assert.equal(v.run("values['client.event_type']"),'Wedding');
  v.run("globalThis.opened={key:'client.setting',type:'select',role:'client',label:'Setting',options:['Indoors','Outdoors'],allow_other:true};globalThis.openInput=makeInput(opened)");
  assert.equal(v.run('allowsOther(opened)'),true);
  v.run("openInput.value='Other: Terrace'");
  assert.equal(v.run('openInput.value'),'Other: Terrace');
});

test('defaults prefill editable fields, with correction and saved answers taking precedence',async()=>{
  const defaults={'client.name':'Prefilled name','client.date':'2027-06-12','client.start':'14:30','client.signature':'not allowed','client.signing_date':'1900-01-01'};
  const v=await viewer({vendor:false,state:'editable',defaults});
  assert.equal(v.run("values['client.name']"),'Prefilled name');
  assert.equal(v.run("controls.find(c=>c.key==='client.name').input.value"),'Prefilled name');
  assert.equal(v.run("values['client.start']"),'14:30');
  assert.equal(v.run("values['client.date']"),'2027-06-12');
  assert.equal(v.run("values['client.signature']"),undefined);
  assert.equal(v.run("values['client.signing_date']"),'2026-11-17');
  v.run("updateValue(definition('client.name'),'')");
  assert.equal(v.run("values['client.name']"),'');
  const revised=await viewer({vendor:false,state:'editable',defaults,prefills:{'client.name':'','client.start':'15:45'}});
  assert.equal(revised.run("values['client.name']"),'');
  assert.equal(revised.run("values['client.start']"),'15:45');
  const submitted=await viewer({vendor:false,state:'awaiting_vendor',defaults});
  assert.equal(submitted.run("values['client.name']"),'Jane Smith');
});

test('blocked tab closure keeps the confirmation and explains manual closure',async()=>{
  const v=await viewer({vendor:false,state:'editable',closeBlocked:true});
  v.run('showPage(1,false)');v.get('signing-consent').checked=true;
  await v.get('preview-form').events.submit({preventDefault(){}});
  v.get('signing-success-close').events.click();
  assert.equal(v.context.window.closeCalls,1);
  assert.equal(v.context.window.closed,false);
  assert.equal(v.get('signing-success-dialog').open,true);
  assert.match(v.get('signing-success-description').textContent,/close it manually/);
  v.run('applyWorkflow(workflow)');
  assert.match(v.get('signing-success-description').textContent,/close it manually/);
});

test('unchecked consent explains submission for each role and never sends',async()=>{
  for(const vendor of [false,true]){
    const v=await viewer({vendor,state:vendor?'awaiting_vendor':'editable'});
    v.run('showPage(1,false)');
    await v.get('preview-form').events.submit({preventDefault(){}});
    assert.match(v.get('preview-notice').textContent,vendor?/countersignature and completing/:/answers and electronic signature to the vendor/);
    assert.match(v.get('preview-notice').textContent,/check the box/);
    assert.equal(v.calls.filter(c=>c.options?.method==='POST').length,0);
  }
});

test('locked defaults are visible but uneditable and correction prefills cannot replace them',async()=>{
  const v=await viewer({vendor:false,state:'editable',defaults:{'client.provided_name':'Fixed name','client.start':'14:30'},lockedFields:['client.provided_name','client.start'],prefills:{'client.provided_name':'Changed'}});
  assert.equal(v.run("values['client.provided_name']"),'Fixed name');
  for(const key of ['client.provided_name','client.start']){
    assert.equal(v.run(`controls.find(c=>c.key==='${key}').input.disabled`),true);
    v.run(`focusField('${key}')`);
    assert.equal(v.run('floating'),null);
  }
  assert.equal(v.run("controls.find(c=>c.key==='client.name').input.disabled"),false);
});

test('numbers have a nonnegative minimum and existing time labels use event wording',async()=>{
  const v=await viewer({vendor:false,state:'editable',labels:{'client.start':'Client start time'}});
  const number=v.run("makeInput({key:'client.amount',type:'number',role:'client',required:true,label:'Amount'})");
  assert.equal(number.type,'number');
  assert.equal(number.min,'0');
  assert.equal(number.step,'any');
  assert.equal(number.inputMode,'decimal');
  assert.equal(v.run("definition('client.start').label"),'Event starting time');
  assert.equal(v.run("controls.find(c=>c.key==='client.start').input.attributes['aria-label']"),'Event starting time');
});

const calculatedFields = [
  {key:'client.end',type:'time',role:'client',label:'Event ending time',required:true},
  {key:'client.week_day',type:'text',role:'client',label:'Weekday',required:true,locked:true,calc:{function:'weekday',sources:['client.date']}},
  {key:'client.total_time',type:'number',role:'client',label:'Total hours',required:true,locked:true,calc:{function:'duration_hours',sources:['client.start','client.end']}},
];
test('calculated fields update on edits and clearing, and show overnight assumptions',async()=>{
  const v=await viewer({vendor:false,state:'editable',extraFields:calculatedFields});
  for(const key of ['client.week_day','client.total_time']) assert.equal(v.run(`controls.find(c=>c.key==='${key}').input.disabled`),true);
  v.run("updateValue(definition('client.date'),'2026-11-30');updateValue(definition('client.start'),'23:00');updateValue(definition('client.end'),'01:30')");
  assert.equal(v.run("values['client.week_day']"),'Monday');
  assert.equal(v.run("values['client.total_time']"),'2.5');
  assert.equal(v.run("controls.find(c=>c.key==='client.total_time').input.value"),'2.5');
  assert.match(v.get('calculation-notes').textContent,/following day \(2.5 hours\)/);
  v.run("updateValue(definition('client.start'),'00:00')");
  assert.equal(v.run("values['client.total_time']"),'1.5');
  assert.equal(v.get('calculation-notes').hidden,true);
  v.run("updateValue(definition('client.end'),'00:00')");
  assert.equal(v.run("values['client.total_time']"),'');
  assert.match(v.run("errors['client.end']"),/different start and end/);
  v.run("updateValue(definition('client.date'),'2027-02-29')");
  assert.equal(v.run("values['client.week_day']"),'');
  v.run("updateValue(definition('client.date'),'2024-02-29')");
  assert.equal(v.run("values['client.week_day']"),'Thursday');
  v.run("updateValue(definition('client.date'),'');updateValue(definition('client.start'),'')");
  assert.equal(v.run("values['client.week_day']"),'');
  assert.equal(v.run("values['client.total_time']"),'');
});

test('calculation initializes from defaults and correction answers rather than stale results',async()=>{
  for(const options of [
    {defaults:{'client.date':'2026-11-30','client.start':'19:00','client.end':'21:15'}},
    {prefills:{'client.date':'2026-11-30','client.start':'19:00','client.end':'21:15','client.week_day':'Wrong','client.total_time':'999'}},
  ]){
    const v=await viewer({vendor:false,state:'editable',extraFields:calculatedFields,...options});
    assert.equal(v.run("values['client.week_day']"),'Monday');
    assert.equal(v.run("values['client.total_time']"),'2.25');
    assert.equal(v.run('dirty'),false);
  }
});
