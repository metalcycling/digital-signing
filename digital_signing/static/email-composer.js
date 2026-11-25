// Shared email editor for workspace and rendered-document send dialogs.
(() => {
  const form = document.getElementById('send-form');
  const panel = document.createElement('section');
  panel.className = 'email-composer';
  panel.innerHTML = `
    <h3>Email messages</h3>
    <p>Edit these messages for this agreement, or save them as your defaults for future agreements. The signature-request body uses the subject title you enter; the greeting updates as you enter the client’s name. Saved defaults keep automatic personalization. The subject title appears bold in the invitation; the agreement name appears bold in the completion email. The private link and expiration notice are added automatically.</p>
    <label for="invitation-title">Email subject</label>
    <div class="email-subject"><span>Signature request for</span><textarea id="invitation-title" name="agreement_subject_title" rows="1" autocomplete="off" maxlength="200" required aria-label="Subject title after Signature request for"></textarea></div>
    <label for="invitation-body">Signature request — greeting, message, and closing</label>
    <textarea id="invitation-body" rows="8" maxlength="10000" required></textarea><p>Formatted message preview</p><div id="invitation-preview" class="email-body-preview"></div>
    <label for="signing-days">Signing link expires after (days)</label>
    <input id="signing-days" type="number" min="1" max="365" step="1" required>
    <details><summary>Completed-agreement email</summary>
    <p>This message will be created automatically when you countersign. Its new link expires this many days after completion.</p>
    <label for="completion-body">Completed agreement — greeting, message, and closing</label>
    <textarea id="completion-body" rows="8" maxlength="10000" required></textarea><p>Formatted message preview</p><div id="completion-preview" class="email-body-preview"></div>
    <label for="completion-days">Completed-agreement link expires after (days)</label>
    <input id="completion-days" type="number" min="1" max="365" step="1" required>
    </details>
    <button id="save-email-defaults" type="button" class="text-button">Save messages & expirations as defaults</button>
    <p id="email-defaults-status" role="status"></p>`;
  form.insertBefore(panel, document.getElementById('send-error'));
  panel.addEventListener('invalid', event => { event.target.closest('details')?.setAttribute('open',''); }, true);
  const $ = id => document.getElementById(id);
  $('invitation-title').addEventListener('keydown', event => { if (event.key === 'Enter') event.preventDefault(); });
  const fields = {'invitation_title':'invitation-title','invitation_body':'invitation-body','completion_body':'completion-body','signing_days':'signing-days','completion_days':'completion-days'};
  const templates = {};
  let names = {recipient_name:'', contract_name:''};
  function segments(template) {
    return template.split(/(\{(?:recipient_name|contract_name|invitation_subject)\})/).filter(Boolean).map(raw => ({raw, text:/^\{(?:recipient_name|contract_name|invitation_subject)\}$/.test(raw) ? names[raw.slice(1,-1)] || raw : raw}));
  }
  function preview(key) {
    if (key === 'invitation_title') return;
    const target = $(key === 'invitation_body' ? 'invitation-preview' : 'completion-preview');
    target.replaceChildren();
    for (const part of segments(templates[key])) {
      const element = document.createElement(['{contract_name}','{invitation_subject}'].includes(part.raw) ? 'strong' : 'span');
      element.textContent = part.text; target.append(element);
    }
  }
  function paint(key) { $(fields[key]).value = segments(templates[key]).map(s => s.text).join(''); preview(key); }
  // Preserve placeholder identity while editing the surrounding personalized copy.
  // Editing a name directly in the body intentionally makes that occurrence literal.
  function capture(key) {
    const parts = segments(templates[key]);
    const before = parts.map(s => s.text).join(''), after = $(fields[key]).value;
    if (before === after) return;
    let start = 0, end = 0;
    while (start < before.length && start < after.length && before[start] === after[start]) start++;
    while (end < before.length-start && end < after.length-start && before[before.length-1-end] === after[after.length-1-end]) end++;
    function slice(from, to) {
      let offset = 0, result = '';
      for (const part of parts) {
        const left = Math.max(from,offset), right = Math.min(to,offset+part.text.length);
        if (left < right) result += left === offset && right === offset+part.text.length ? part.raw : part.text.slice(left-offset,right-offset);
        offset += part.text.length;
      }
      return result;
    }
    templates[key] = slice(0,start) + after.slice(start,after.length-end) + slice(before.length-end,before.length);
  }
  const bodyKeys = ['invitation_title','invitation_body','completion_body'];
  function updateSubject() {
    capture('invitation_body');
    names.invitation_subject = segments(templates.invitation_title).map(s => s.text).join('').trim();
    paint('invitation_body');
  }
  for (const key of bodyKeys) $(fields[key]).addEventListener('input', () => {
    if (templates[key] !== undefined) {
      capture(key);
      if (key === 'invitation_title') updateSubject();
      else preview(key);
    }
  });
  $('recipient-name').addEventListener('input', () => {
    if (!templates.invitation_body) return;
    for (const key of bodyKeys) capture(key);
    names.recipient_name = $('recipient-name').value.trim();
    names.invitation_subject = segments(templates.invitation_title).map(s => s.text).join('').trim();
    for (const key of bodyKeys) paint(key);
  });
  function values() {
    for (const key of bodyKeys) capture(key);
    return Object.fromEntries(Object.entries(fields).map(([key,id]) => [key,key.endsWith('_days') ? Number($(id).value) : templates[key]]));
  }
  async function request(options) {
    const response = await fetch('/api/email-defaults',options);
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Enter both messages and whole-number expiration periods from 1 to 365 days.');
    return data;
  }
  window.emailComposer = {values, async load(contractName) {
    const deliveryResponse = await fetch('/api/email-delivery');
    if (!deliveryResponse.ok) throw new Error('Unable to check email delivery settings.');
    const delivery = await deliveryResponse.json();
    $('send-confirm').textContent = delivery.mode === 'gmail' ? 'Send email' : 'Create test email';
    $('send-mode-note').textContent = delivery.mode === 'gmail' ? 'This sends a real email through Gmail. Localhost links open only on this computer.' : 'Local test: the email will appear in the outbox. No email will be sent.';
    const data = await request();
    names = {recipient_name:$('recipient-name').value.trim(), contract_name:contractName, invitation_subject:''};
    for (const [key,id] of Object.entries(fields)) {
      if (!key.endsWith('_days')) { templates[key] = key === 'invitation_title' ? '' : key === 'invitation_body' ? data[key].replaceAll('{contract_name}', '{invitation_subject}') : data[key]; paint(key); }
      else $(id).value = data[key];
    }
    $('email-defaults-status').textContent = '';
  }};
  $('save-email-defaults').addEventListener('click',async () => {
    // Only validate message settings; recipient details can be filled later.
    for (const id of Object.values(fields)) {
      if (!$(id).checkValidity()) { $(id).closest('details')?.setAttribute('open',''); $(id).reportValidity(); return; }
    }
    $('save-email-defaults').disabled = true;
    try {
      await request({method:'PUT',headers:{'Content-Type':'application/json','X-Workspace-Request':'1'},body:JSON.stringify(values())});
      $('email-defaults-status').textContent = 'Defaults saved for future agreements. Existing invitations keep their original settings.';
    } catch(error) { $('email-defaults-status').textContent = error.message; }
    finally { $('save-email-defaults').disabled = false; }
  });
})();
