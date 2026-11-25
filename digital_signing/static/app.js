const $ = (id) => document.getElementById(id);
let mode = 'demo';
let busy = false;
let selectedContract = null;
let rendering = false;
function notify(message) { $('notice').textContent = message; }
async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (response.status === 401) location.assign('/login?' + new URLSearchParams({next:location.pathname+location.search}));
  if (!response.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : Array.isArray(data.detail) ? data.detail.map(item => item.msg).join(' ') : 'Unable to complete this request.';
    throw new Error(detail);
  }
  return data;
}
function selectContract(contract) {
  selectedContract = contract;
  $('preview-link').hidden = true;
  $('review-link').hidden = true;
  $('sign-link').hidden = contract.status !== 'awaiting_vendor';
  $('final-link').hidden = contract.status !== 'completed';
  $('revision').hidden = contract.status === 'draft';
  if (contract.latest_version) {
    $('sign-link').href = `/countersign/${contract.id}?version=${contract.latest_version}`;
    $('final-link').href = `/api/contracts/${contract.id}/submissions/${contract.latest_version}/final.pdf`;
  }
  loadPreviewLink(contract.id);
  $('selected-name').textContent = contract.name;
  $('selected-description').textContent = mode === 'demo' ? 'Demo draft saved locally. No Google document was copied.' : 'Your draft is ready. Edit it in Google Docs before rendering.';
  if (contract.status === 'awaiting_vendor') $('selected-description').textContent = 'The client has submitted. Review their saved PDF and add your countersignature.';
  if (contract.status === 'sent') $('selected-description').textContent = 'The invitation and delivery status are in the email outbox. This version is locked.';
  if (contract.status === 'completed') $('selected-description').textContent = 'Both signatures are saved. Download the final PDF, or create a revision for changes.';
  const link = $('document-link');
  link.hidden = !contract.document_url;
  if (contract.document_url) link.href = contract.document_url;
  else link.removeAttribute('href');
  $('render').disabled = rendering || contract.status !== 'draft';
}
async function loadContracts() {
  const savedScroll = $('contracts').scrollTop;
  const state = await api('/api/workspace');
  mode = state.mode;
  $('mode').textContent = mode === 'demo' ? 'DEMO MODE' : 'GOOGLE DRIVE';
  const container = $('contracts');
  container.replaceChildren();
  if (!state.contracts.length) {
    const empty = document.createElement('p'); empty.className = 'empty';
    empty.textContent = 'No agreements yet. Create your first draft above.'; container.append(empty);
  }
  for (const contract of state.contracts) {
    const row = document.createElement('div'); row.className = 'contract-row';
    const info = document.createElement('div'); const title = document.createElement('strong');
    title.textContent = contract.name;
    const meta = document.createElement('small'); meta.textContent = `${contract.id} · ${new Date(contract.created_at).toLocaleDateString()}`;
    info.append(title, meta);
    const actions = document.createElement('div'); actions.className = 'row-actions';
    const badge = document.createElement('span'); badge.className = 'badge'; badge.textContent = ({draft:mode === 'demo'?'DEMO DRAFT':'DRAFT',sent:'AWAITING CLIENT',awaiting_vendor:'AWAITING YOUR SIGNATURE',completed:'FULLY EXECUTED'})[contract.status] || contract.status;
    const button = document.createElement('button'); button.className = 'text-button'; button.textContent = 'Select';
    button.addEventListener('click', () => selectContract(contract));
    if (contract.status === 'awaiting_vendor') {
      const sign = document.createElement('a'); sign.className='text-button'; sign.textContent='Review & sign'; sign.href=`/countersign/${contract.id}?version=${contract.latest_version}`; actions.append(sign);
    }
    const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'text-button danger-text'; remove.textContent = 'Delete';
    remove.addEventListener('click', () => openDelete(contract));
    const copy = document.createElement('button'); copy.type='button'; copy.className='text-button'; copy.textContent='Copy link';
    copy.disabled = contract.status !== 'completed' || mode !== 'google';
    copy.title = mode !== 'google' ? 'Google Drive links are unavailable in demo mode.' : contract.status !== 'completed' ? 'Available after both parties sign.' : 'Copy the fully executed PDF’s Google Drive link';
    copy.addEventListener('click', async () => {
      copy.disabled=true;
      try {
        const url = contract.final_drive_url || (await api(`/api/contracts/${contract.id}/submissions/${contract.latest_version}/drive`,{method:'POST',headers:{'X-Workspace-Request':'1'}})).url;
        contract.final_drive_url=url;
        try { await navigator.clipboard.writeText(url); notify('Google Drive PDF link copied.'); }
        catch { window.prompt('Copy this Google Drive PDF link:',url); }
      } catch(error) { notify(error.message); }
      finally { copy.disabled=false; }
    });
    if (contract.status === 'completed' && contract.drive_save_error) {
      const warning=document.createElement('small'); warning.textContent='PDF not yet saved to Drive. Use Copy link to retry.'; info.append(warning);
    }
    actions.append(badge, button, copy, remove); row.append(info, actions); container.append(row);
  }
  $('contracts').scrollTop = savedScroll;
  if (selectedContract) { const fresh=state.contracts.find(c=>c.id===selectedContract.id); if(fresh && fresh.status!==selectedContract.status) selectContract(fresh); else if(!fresh) clearSelection(); }
}
async function loadTemplates() {
  const templates = await api('/api/templates');
  $('template').replaceChildren(new Option(templates.length ? 'Choose a template' : 'No Google Docs templates found', ''));
  for (const template of templates) $('template').append(new Option(template.name, template.id));
  $('template').disabled = !templates.length;
  $('create').disabled = !templates.length;
}
$('create-form').addEventListener('submit', async (event) => {
  event.preventDefault(); if (busy) return;
  busy = true; $('create').disabled = true; notify('Creating your agreement…');
  // Open during the user gesture so browsers do not block the eventual Docs tab.
  const tab = mode === 'google' ? window.open('about:blank', '_blank') : null;
  if (tab) tab.opener = null;
  try {
    const contract = await api('/api/contracts', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Workspace-Request': '1'}, body: JSON.stringify({name: $('name').value, template_id: $('template').value})});
    selectContract(contract);
    if (tab && contract.document_url) tab.location.replace(contract.document_url);
    else if (tab) tab.close();
    notify(mode === 'demo' ? 'Demo draft created. Connect Google Drive to copy and edit real documents.' : 'Agreement created. Edit the copy in Google Docs. You can reopen it using the link on the right.');
    $('name').value = '';
    await loadContracts();
  } catch (error) { if (tab) tab.close(); notify(error.message); }
  finally { busy = false; $('create').disabled = false; }
});
async function loadPreviewLink(id) {
  try {
    const versions = await api(`/api/contracts/${encodeURIComponent(id)}/renders`);
    if (selectedContract?.id !== id) return;
    if (versions.length) {
      $('preview-link').href = `/preview/${encodeURIComponent(id)}?version=${versions[0].version}`;
      $('preview-link').hidden = false;
      $('review-link').href = $('preview-link').href + '&review=1';
      $('review-link').hidden = false;
    }
  } catch(error) { notify(error.message); }
}
$('render').addEventListener('click', async () => {
  if (!selectedContract || rendering) return;
  const id = selectedContract.id;
  rendering = true; $('render').disabled = true;
  const tab = window.open('about:blank', '_blank');
  if (tab) tab.opener = null;
  notify('Reading the saved document and building fillable fields…');
  try {
    const result = await api(`/api/contracts/${encodeURIComponent(id)}/renders`, {method:'POST', headers:{'X-Workspace-Request':'1'}});
    if (tab) tab.location.replace(result.preview_url + '&review=1');
    await loadPreviewLink(id);
    notify(`Version ${result.version} rendered with ${result.field_count} unique fields. Open the saved preview to review and try the form.`);
  } catch(error) { if (tab) tab.close(); notify(error.message); }
  finally { rendering = false; $('render').disabled = !selectedContract || selectedContract.status !== 'draft'; }
});
$('refresh').addEventListener('click', async () => { try { await loadContracts(); await loadTemplates(); } catch(error) { notify(error.message); } });
(async () => { try { await loadContracts(); if(mode === 'demo') notify('Demo mode: try the workflow with local drafts. Google copying requires the one-time setup in README.md.'); await loadTemplates(); } catch(error) { notify(error.message); } })();

function buildTag() {
  const key = $('tag-key').value.trim();
  const type = $('tag-type').value;
  const label = $('tag-label').value.trim();
  const options = $('tag-options').value.trim();
  const automaticDate = type === 'signing_date' || (type === 'date' && ['signing_date','signed_date','signature_date','singning_date'].includes(key.split('.').pop()));
  const supportsDefault = !['signature','initials'].includes(type) && !automaticDate;
  $('tag-default-wrap').hidden = !supportsDefault;
  $('tag-locked-wrap').hidden = !supportsDefault;
  const lockField = supportsDefault && $('tag-locked').checked;
  const defaultValue = supportsDefault ? $('tag-default').value.trim() : '';
  $('tag-default-hint').textContent = ({date:'Use YYYY-MM-DD, for example 2027-06-12.',time:'Use HH:MM in 24-hour format with 15-minute intervals, for example 14:30 (2:30 PM).',checkbox:'Use true for checked or false for unchecked. Required boxes must still be checked before submitting.',select:'Use an exact listed choice, or Other: your answer when custom answers are enabled.'})[type] || (lockField ? 'This answer cannot be changed after rendering.' : 'An editable starting answer, not a placeholder.');
  $('tag-options-wrap').hidden = type !== 'select';
  let error = '';
  if (!/^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$/.test(key)) error = 'Use a field name such as client.name (lowercase letters, digits, dots, underscores).';
  if (/[{}|]/.test(label + options + defaultValue)) error = 'Labels, options, and prefilled answers cannot contain braces or pipes.';
  const choices = options.split(';').map(v => v.trim());
  if (type === 'select' && (choices.length < 2 || choices.length > 50 || choices.some(v => !v || v.length > 100) || new Set(choices).size !== choices.length)) error = 'Provide 2–50 distinct dropdown choices, separated by semicolons.';
  if (label.length > 150) error = 'Keep the label under 151 characters.';
  if (lockField && !defaultValue) error = 'A locked field needs a valid prefilled answer.';
  const pieces = [key, type];
  if (lockField) pieces.push('locked=true');
  if (label) pieces.push(`label=${label}`);
  if (type === 'select') pieces.push(`options=${choices.join(';')}`, `other=${$('tag-other').checked ? 'true' : 'false'}`);
  if (defaultValue) pieces.push(`default=${defaultValue}`);
  if (!$('tag-required').checked) pieces.push('optional');
  const defaultRole = key.startsWith('vendor.') ? 'vendor' : 'client';
  if ($('tag-role').value !== defaultRole) pieces.push(`role=${$('tag-role').value}`);
  $('tag-output').textContent = error || `{{${pieces.join('|')}}}`;
  $('copy-tag').disabled = Boolean(error);
  $('copy-result').textContent = '';
}
for (const id of ['tag-key', 'tag-type', 'tag-label', 'tag-options', 'tag-other', 'tag-default', 'tag-locked', 'tag-required', 'tag-role']) $(id).addEventListener('input', buildTag);
$('copy-tag').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText($('tag-output').textContent); $('copy-result').textContent = 'Copied. Paste into Google Docs.'; }
  catch { $('copy-result').textContent = 'Select the tag above and copy it manually.'; }
});
buildTag();

$('revision').addEventListener('click', async () => {
  if (!selectedContract || !confirm('Create a new revision from the latest Google Doc? Previous submissions stay saved, and the client must fill and sign the new version.')) return;
  $('revision').disabled = true;
  try {
    const result = await api(`/api/contracts/${selectedContract.id}/revisions`, {method:'POST', headers:{'X-Workspace-Request':'1'}});
    await loadContracts(); await loadPreviewLink(selectedContract.id);
    notify(`Revision ${result.version} is ready. Previous signed versions remain unchanged.`);
  } catch(error) {notify(error.message);}
  finally {$('revision').disabled=false;}
});
async function loadNotifications() {
  const savedScroll = $('notification-list').scrollTop;
  const items=await api('/api/notifications');
  $('notifications').hidden=!items.length; $('notification-list').replaceChildren();
  for(const item of items) {
    const row=document.createElement('p'), link=document.createElement('a');
    link.textContent=`${item.name} · v${item.version} — ${item.kind==='client_submitted'?'Client submitted; ready for your signature':item.kind==='vendor_signed'?'Fully executed PDF ready':item.kind==='corrections_requested'?'Corrections requested; awaiting client':'New revision created'}`;
    link.href=item.kind==='client_submitted'?`/countersign/${item.contract_id}?version=${item.version}`:`/preview/${item.contract_id}?version=${item.version}`;
    row.append(link); $('notification-list').append(row);
  }
  $('notification-list').scrollTop = savedScroll;
}
loadNotifications().catch(error=>notify(error.message));
setInterval(()=> { if(!document.hidden) Promise.all([loadContracts(),loadNotifications(),loadOutbox()]).catch(()=>{}); },15000);

let deletingContract = null;
let deleting = false;
function clearSelection() {
  selectedContract = null;
  $('selected-name').textContent = 'Your next step';
  $('selected-description').textContent = 'Create an agreement or select one below to continue.';
  for (const id of ['document-link','preview-link','review-link','sign-link','final-link','revision']) $(id).hidden = true;
  $('render').disabled = true;
}
function openDelete(contract) {
  if (deleting) return;
  deletingContract = contract;
  $('delete-contract-name').textContent = contract.name;
  $('delete-confirmation').value = ''; $('delete-error').textContent = '';
  $('delete-confirm').disabled = true;
  $('delete-dialog').showModal(); $('delete-cancel').focus();
}
$('delete-confirmation').addEventListener('input', () => {
  $('delete-confirm').disabled = deleting || $('delete-confirmation').value !== deletingContract?.name;
});
$('delete-cancel').addEventListener('click', () => $('delete-dialog').close());
$('delete-dialog').addEventListener('cancel', event => { if (deleting) event.preventDefault(); });
$('delete-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (deleting || !deletingContract || $('delete-confirmation').value !== deletingContract.name) return;
  deleting = true; $('delete-confirm').disabled = true; $('delete-cancel').disabled = true;
  $('delete-error').textContent = 'Deleting agreement…';
  try {
    await api(`/api/contracts/${encodeURIComponent(deletingContract.id)}`, {method:'DELETE', headers:{'Content-Type':'application/json','X-Workspace-Request':'1'}, body:JSON.stringify({confirmation:$('delete-confirmation').value})});
    if (selectedContract?.id === deletingContract.id) clearSelection();
    $('delete-dialog').close(); notify('Agreement permanently deleted.');
    await Promise.all([loadContracts(), loadNotifications(), loadOutbox()]);
  } catch (error) { $('delete-error').textContent = error.message; notify(error.message); }
  finally { deleting = false; $('delete-cancel').disabled = false; $('delete-confirm').disabled = $('delete-confirmation').value !== deletingContract?.name; }
});

async function loadOutbox() {
  const savedScroll = $('outbox-list').scrollTop;
  const expanded = new Set([...$('outbox-list').querySelectorAll('details[open]')].map(item=>item.dataset.emailId));
  const delivery = await api('/api/email-delivery');
  $('outbox-mode').textContent = delivery.mode === 'gmail' ? 'Gmail delivery enabled. Check delivery status below. Localhost links open only on this computer.' : 'Local test mode: emails are saved here, not sent.';
  const items = await api('/api/outbox');
  $('outbox-list').replaceChildren();
  if (!items.length) { $('outbox-list').textContent = 'No emails yet.'; return; }
  for (const item of items) {
    const details = document.createElement('details'); details.dataset.emailId=String(item.id); details.open=expanded.has(String(item.id));
    const summary = document.createElement('summary'); summary.textContent = `${item.subject} — ${item.recipient_email || 'Vendor (Gmail sender)'}`;
    const meta = document.createElement('p'); meta.textContent = `Version ${item.version} · ${new Date(item.created_at).toLocaleString()} · ${{local:'Saved locally, not delivered',pending:'Ready to send',sending:'Sending',sent:'Accepted by Gmail',failed:'Failed',unknown:'Delivery uncertain'}[item.delivery_status] || 'Saved locally'}`;
    const body = document.createElement('div'); body.className = 'email-body-preview';
    if (item.html_body) body.innerHTML = item.html_body; // Server escapes all user text before adding email formatting.
    else body.textContent = item.body.replace(item.link, new URL(item.link, location.origin).href);
    const link = document.createElement('a'); link.href = item.link; link.textContent = item.kind === 'vendor_alert' ? 'Review & countersign ↗' : ['completed','completed_vendor'].includes(item.kind) ? 'Open completed agreement ↗' : 'Open client signing link ↗'; link.target='_blank'; link.rel='noopener noreferrer';
    const copy = document.createElement('button'); copy.type='button'; copy.className='text-button'; copy.textContent='Copy link';
    copy.addEventListener('click', async () => { try { await navigator.clipboard.writeText(new URL(item.link,location.origin).href); copy.textContent='Copied'; } catch { notify('Copy the signing link using the link above.'); } });
    const error = document.createElement('p'); error.textContent = item.delivery_error || '';
    details.append(summary,meta,error,body,link,copy);
    if (item.attach_final_pdf) { const attachment=document.createElement('p');attachment.textContent='Attachment: fully executed agreement (PDF)';details.append(attachment); }
    if (delivery.mode === 'gmail' && !['sent','sending'].includes(item.delivery_status)) {
      const retry = document.createElement('button'); retry.type='button'; retry.className='text-button'; retry.textContent=item.delivery_status==='local'?'Send via Gmail':'Retry sending';
      retry.addEventListener('click',async()=>{
        if (!confirm(`${item.delivery_status==='unknown'?'Delivery was uncertain. Check Gmail Sent first to avoid a duplicate. ':''}Send this email to ${item.recipient_email}?`)) return;
        retry.disabled=true;
        try { const result=await api(`/api/outbox/${item.id}/send`,{method:'POST',headers:{'X-Workspace-Request':'1'}}); notify(result.delivery_status==='sent'?'Email accepted by Gmail.':result.delivery_error || 'Email not sent.'); await loadOutbox(); }
        catch(error) {notify(error.message);retry.disabled=false;}
      });
      details.append(retry);
    }
     $('outbox-list').append(details);
  }
  $('outbox-list').scrollTop = savedScroll;
}
loadOutbox().catch(error=>notify(error.message));

function selectWorkspaceTab(key) {
  for (const name of ['contracts','tags']) {
    const active = name === key;
    $(`${name}-panel`).hidden = !active;
    $(`${name}-tab`).setAttribute('aria-selected',String(active));
    $(`${name}-tab`).tabIndex = active ? 0 : -1;
  }
}
for (const name of ['contracts','tags']) {
  $(`${name}-tab`).addEventListener('click', () => { location.hash=name; selectWorkspaceTab(name); });
  $(`${name}-tab`).addEventListener('keydown', event => {
    if (!['ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key==='Home'?'contracts':event.key==='End'?'tags':name==='contracts'?'tags':'contracts';
    location.hash=next; selectWorkspaceTab(next); $(`${next}-tab`).focus();
  });
}
window.addEventListener('hashchange',()=>selectWorkspaceTab(location.hash==='#tags'?'tags':'contracts'));
selectWorkspaceTab(location.hash==='#tags'?'tags':'contracts');
