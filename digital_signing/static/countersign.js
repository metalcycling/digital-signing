const $ = id => document.getElementById(id);
const id = location.pathname.split('/').pop();
let version, state, fields;
const values = Object.create(null), inputs = new Map();
const signatureEditor = new window.SignatureEditor();
async function api(url, options={}) {
  const response=await fetch(url,options), data=await response.json();
  if(!response.ok) {
    const errors=data.detail?.errors || {};
    for(const [key,input] of inputs) input.setAttribute('aria-invalid',errors[key]?'true':'false');
    throw new Error(typeof data.detail==='string'?data.detail:data.detail?.message || 'Unable to save the countersignature.');
  }
  return data;
}
function signed(result) {
  if(result.state!=='completed') return;
  $('vendor-form').hidden=true; $('completed').hidden=false; $('final-pdf').href=result.final_pdf_url;
}
function signatureButton(field) {
  const button=document.createElement('button'); button.type='button'; button.className='signature-field'; button.textContent='Add your signature';
  button.addEventListener('click',()=>signatureEditor.open({label:field.label, value:values[field.key], anchor:button,
    onApply:value=> {values[field.key]=value; button.replaceChildren(); if(value) {const image=document.createElement('img'); image.src=value.image; image.alt='Your signature'; button.append(image);} else button.textContent='Add your signature';}}));
  return button;
}
$('vendor-form').addEventListener('submit',async event=> {
  event.preventDefault(); $('finalize').disabled=true; $('notice').textContent='Saving the countersignature and generating the final PDF…';
  const payload={};
  for(const f of fields) payload[f.key]=values[f.key] ?? (f.type==='checkbox'?false:'');
  try {
    const result=await api(`/api/contracts/${id}/renders/${version}/countersign`,{method:'POST',headers:{'Content-Type':'application/json','X-Workspace-Request':'1'},body:JSON.stringify({values:payload,signer_name:$('vendor-name').value,consent:$('vendor-consent').checked})});
    signed(result); $('notice').textContent='The final agreement has been saved. The client can now download it from their confirmation page.';
  } catch(error) {$('notice').textContent=error.message;}
  finally {$('finalize').disabled=false;}
});
(async()=> {
  try {
    const versions=await api(`/api/contracts/${id}/renders`);
    version=new URLSearchParams(location.search).get('version') || versions[0]?.version;
    const base=`/api/contracts/${id}/renders/${version}`;
    state=await api(base+'/workflow');
    const snapshot=await api(base); $('title').textContent=snapshot.name;
    if(!state.client) throw new Error('This agreement has not been submitted by the client yet.');
    $('client-summary').textContent=`Submitted by ${state.client.signer_name} on ${new Date(state.client.signed_at).toLocaleString()}. Version ${version}.`;
    $('client-pdf').href=state.client_pdf_url;
    for(const field of snapshot.fields.filter(f=>f.role==='client')) {
      const dt=document.createElement('dt');dt.textContent=field.label;
      const dd=document.createElement('dd'), value=state.client.values[field.key];
      dd.textContent=typeof value==='object'&&value ? `Signature (${value.method})` : typeof value==='boolean' ? value?'Yes':'No' : value || '(blank)';
      $('answers').append(dt,dd);
    }
    if(state.superseded && state.state!=='completed') throw new Error('A newer revision supersedes this submission. Return to the workspace to review the current version.');
    fields=state.vendor_fields;
    $('placement-note').textContent=snapshot.fields.some(f=>f.role==='vendor'&&f.type==='signature') ? 'Your signature will fill the vendor field and appear in the completion record.' : 'This template has no vendor signature tag. Your signature will appear on the attached completion record; the original pages remain unchanged.';
    $('consent-text').textContent=state.vendor_consent;
    for(const field of fields) {
      const label=document.createElement('label');label.textContent=field.label;
      let input;
      if(field.type==='signature') input=signatureButton(field);
      else {
        input=document.createElement(field.type==='select'?'select':field.type==='textarea'?'textarea':'input');
        if(field.type==='select') {input.append(new Option('Choose…',''));for(const opt of field.options) input.append(new Option(opt,opt));}
        else if(field.type!=='textarea') input.type=({phone:'tel',initials:'text'})[field.type]||field.type;
        if(field.type==='number') input.step='any';
        input.required=field.required;
        if(field.type==='date') {input.value=new Date().toISOString().slice(0,10);values[field.key]=input.value;}
        input.addEventListener('input',()=>{values[field.key]=input.type==='checkbox'?input.checked:input.value;});
      }
      input.id=`vendor-field-${inputs.size}`; label.htmlFor=input.id; inputs.set(field.key,input);
      $('vendor-fields').append(label,input);
    }
    signed(state);
  } catch(error) {$('notice').textContent=error.message;$('vendor-form').hidden=true;}
})();
