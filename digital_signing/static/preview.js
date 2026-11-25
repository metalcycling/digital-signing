const $ = (id) => document.getElementById(id);
const contractId = location.pathname.split('/').pop();
const isRecipient = location.pathname.startsWith('/sign/');
const base = isRecipient ? `/api/sign/${encodeURIComponent(contractId)}` : `/api/contracts/${encodeURIComponent(contractId)}/renders`;
let snapshot;
let workflow;
let locked = false;
let dirty = false;
let currentPage = 0;
const isVendor = location.pathname.startsWith('/countersign/');
const activeRole = isVendor ? 'vendor' : 'client';
const isReview = !isRecipient && !isVendor && new URLSearchParams(location.search).get('review') === '1';
const values = Object.create(null);
const errors = Object.create(null);
const controls = [];
const pageViews = [];
let serial = 0;
let floating = null;
let restoringFocus = false;
const floatingEditor = document.createElement('section');
floatingEditor.className = 'floating-field-editor';
floatingEditor.hidden = true;
floatingEditor.setAttribute('role', 'dialog');
floatingEditor.setAttribute('aria-labelledby', 'floating-field-title');
document.body.append(floatingEditor);
const signatureEditor = new window.SignatureEditor();
async function api(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (response.status === 401) location.assign('/login?' + new URLSearchParams({next:location.pathname+location.search}));
  if (!response.ok) {
    const error = new Error(typeof data.detail === 'string' ? data.detail : data.detail?.message || 'Unable to process these values.');
    error.fields = data.detail?.errors; throw error;
  }
  return data;
}
function notice(message) { $('preview-notice').textContent = message; $('preview-notice').hidden = !message; }
function post(body) { return {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Workspace-Request': '1'}, ...(body ? {body: JSON.stringify(body)} : {})}; }
function definition(key) { return snapshot.fields.find(f => f.key === key) || workflow.vendor_fields.find(f => f.key === key); }
function isLocked(state) { return state.superseded || (isReview && state.sent) || state.state !== (isVendor ? 'awaiting_vendor' : 'editable'); }
function isSigningDate(field) { return field.type === 'signing_date' || (field.type === 'date' && ['signing_date','signed_date','signature_date','singning_date'].includes(field.key.split('.').pop())); }
function canEdit(field) { return !locked && field.role === activeRole && !isSigningDate(field); }
function signingFields() { return isVendor ? workflow.vendor_fields : snapshot.fields.filter(f => f.role === 'client'); }
function showErrors(fieldErrors) {
  for (const key of Object.keys(errors)) delete errors[key];
  Object.assign(errors, fieldErrors || {});
  for (const control of controls) control.input.setAttribute('aria-invalid', errors[control.key] ? 'true' : 'false');
  if (Object.keys(errors).length) {
    notice(`Please correct ${Object.keys(errors).length} field(s) highlighted in the document.`);
    focusField(Object.keys(errors)[0]);
  }
}
function allowsOther(field) {
  if (field.type !== 'select') return false;
  if (Object.hasOwn(field,'allow_other')) return field.allow_other === true;
  const key = field.key.toLowerCase().replaceAll('.', '_');
  return field.type === 'select' && (field.allow_other || field.options.some(option=>option.toLowerCase()==='other') || ['event_type','type_of_event','event_category'].some(suffix=>key.endsWith(suffix)) || ['type of event','event type'].includes(field.label.toLowerCase()));
}
function documentButton(field) { return ['date','signing_date','time'].includes(field.type) || allowsOther(field); }
function dateDisplay(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return value;
  const day = new Date(`${value}T12:00:00Z`).toLocaleDateString('en-US',{weekday:'long',timeZone:'UTC'});
  return `${match[2]}-${match[3]}-${match[1]} (${day})`;
}
function makeComposite(field) {
  const group = document.createElement('div'); group.className='composite-editor'; group.setAttribute('role','group');
  const addSelect = (label, options) => {
    const wrap=document.createElement('label'); wrap.textContent=label;
    const select=document.createElement('select'); select.setAttribute('aria-label',label); select.append(new Option('—',''));
    for (const [value,text] of options) select.append(new Option(text,value));
    wrap.append(select); group.append(wrap); return select;
  };
  let read, write, focus;
  if (field.type==='time') {
    group.classList.add('time-editor');
    const hour=addSelect('Hour',Array.from({length:12},(_,i)=>[String(i+1),String(i+1)]));
    const minute=addSelect('Minute',['00','15','30','45'].map(v=>[v,v]));
    const period=addSelect('AM/PM',[['AM','AM'],['PM','PM']]);
    read=()=>hour.value && minute.value && period.value ? `${String(Number(hour.value)%12+(period.value==='PM'?12:0)).padStart(2,'0')}:${minute.value}` : '';
    write=value=>{
      if (!value) {hour.value='';minute.value='';period.value='';return;}
      const [h,m]=value.split(':'); hour.value=String(Number(h)%12||12);minute.value=m;period.value=Number(h)<12?'AM':'PM';
    };
    // Keep partial selections while the user chooses the remaining components.
    for (const part of [hour,minute,period]) part.addEventListener('change',()=>{ const value=read(); group.editing=true;updateValue(field,value);group.editing=false; });
    focus=()=>hour.focus({preventScroll:true});
  } else {
    const choices=field.options.filter(option=>option.toLowerCase()!=='other');
    const choice=addSelect(field.label,[...choices.map(v=>[v,v]),['__other__','Other']]);
    const label=document.createElement('label');label.textContent='Please specify';
    const text=document.createElement('input');text.type='text';text.maxLength=150;text.setAttribute('aria-label','Please specify the event');label.append(text);group.append(label);
    read=()=>choice.value==='__other__'?`Other: ${text.value.trim()}`:choice.value;
    write=value=>{const other=value?.startsWith('Other:') || value?.toLowerCase()==='other';choice.value=other?'__other__':value||'';text.value=other?value.replace(/^Other:\s*/i,'').replace(/^Other$/i,''):'';label.hidden=!other;};
    choice.addEventListener('change',()=>{label.hidden=choice.value!=='__other__';group.editing=true;updateValue(field,read());group.editing=false;if(!label.hidden)text.focus();positionFloating();});
    text.addEventListener('input',()=>{group.editing=true;updateValue(field,read());group.editing=false;});
    focus=()=>choice.focus({preventScroll:true});
  }
  Object.defineProperty(group,'value',{get:read,set:value=>{if(!group.editing)write(value);}});
  group.focus=focus;group.value=values[field.key] || '';
  return group;
}
function displayValue(field) {
  const value = values[field.key];
  if (field.type === 'checkbox') return value ? '✓' : '';
  if (field.type === 'date' || field.type === 'signing_date') return value ? dateDisplay(value) : 'MM-DD-YYYY';
  if (field.type === 'time' && value) {const [h,m]=value.split(':');return `${Number(h)%12||12}:${m} ${Number(h)<12?'AM':'PM'}`;}
  return value || (field.role === 'vendor' ? 'Vendor' : ({select:'Choose…', date:'Date', time:'Time', textarea:'Add text…'})[field.type] || field.label);
}
function updateValue(field, value) {
  values[field.key] = value; dirty = true; delete errors[field.key];
  for (const control of controls.filter(c => c.key === field.key)) {
    if (field.type === 'signature') paintSignature(control.input, field);
    else if (control.button) control.input.textContent = displayValue(field);
    else if (field.type === 'checkbox') control.input.checked = Boolean(value);
    else control.input.value = value;
    if (control.button) {control.input.title = `${field.label}: ${displayValue(field)}`; fitDocumentText(control.input);}
    control.input.removeAttribute('aria-invalid');
  }
  if (floating?.field.key === field.key) {
    if (field.type === 'checkbox') floating.input.checked = Boolean(value);
    else floating.input.value = value;
    $('floating-field-error').textContent = '';
  }
}
function paintSignature(button, field) {
  const value = values[field.key];
  button.replaceChildren();
  if (value?.image) {
    const image = document.createElement('img'); image.src = value.image; image.alt = `${field.label} signature`;
    button.append(image); button.setAttribute('aria-label', `Change ${field.label}`);
  } else {
    button.textContent = field.role === 'vendor' ? 'Vendor signature' : 'Sign here';
    button.setAttribute('aria-label', `Add ${field.label}`);
  }
}
function makeInput(field, onDocument = false) {
  if (documentButton(field) && onDocument) {
    const button = document.createElement('button'); button.type = 'button'; button.id = `field-${++serial}`;
    button.disabled = !canEdit(field); button.textContent = displayValue(field);
    button.setAttribute('aria-label', field.label); button.setAttribute('aria-haspopup', 'dialog');
    button.addEventListener('click', () => openFloating(field, button));
    return button;
  }
  if (field.type === 'signature') {
    const button = document.createElement('button'); button.type = 'button'; button.id = `field-${++serial}`;
    button.className = 'signature-field'; button.disabled = !canEdit(field);
    button.setAttribute('aria-haspopup', 'dialog');
    paintSignature(button, field);
    button.addEventListener('click', () => openFloating(field, button));
    return button;
  }
  let input;
  if (field.type === 'time' || allowsOther(field)) {
    input = makeComposite(field);
  } else if (field.type === 'select') {
    input = document.createElement('select'); input.append(new Option('Choose…', ''));
    for (const option of field.options) input.append(new Option(option, option));
  } else if (field.type === 'textarea') {
    input = document.createElement('textarea'); input.maxLength = 5000;
  } else {
    input = document.createElement('input');
    input.type = ({phone:'tel', initials:'text', signature:'text'})[field.type] || field.type;
    if (field.type === 'number') input.step = 'any';
    input.maxLength = field.type === 'initials' ? 12 : field.type === 'signature' ? 200 : 5000;
  }
  input.id = `field-${++serial}`; input.name = field.key;
  input.setAttribute('aria-label', `${field.label}${field.role === 'vendor' ? ' (vendor)' : ''}`);
  input.disabled = !canEdit(field);
  input.required = field.required && field.role === activeRole;
  input.placeholder = field.role === 'vendor' ? 'Reserved for vendor' : field.label;
  if (field.type === 'signature') input.classList.add('signature-input');
  if (field.type === 'checkbox') input.checked = Boolean(values[field.key]);
  else input.value = values[field.key] || '';
  if (field.type !== 'time' && !allowsOther(field)) input.addEventListener('input', () => updateValue(field, input.type === 'checkbox' ? input.checked : input.value));
  return input;
}
function highlightField(field) {
  for (const control of controls) control.input.classList.toggle('active-field', control.key === field.key);
}
function closeFloating(restore = false) {
  if (!floating) return;
  const anchor = floating.anchor;
  anchor.setAttribute('aria-expanded', 'false');
  floating = null; floatingEditor.hidden = true;
  if (restore) {
    restoringFocus = true; anchor.focus({preventScroll:true}); restoringFocus = false;
  }
}
function positionFloating() {
  if (!floating) return;
  const rect = floating.anchor.getBoundingClientRect();
  if (rect.bottom < 0 || rect.top > window.innerHeight || rect.right < 0 || rect.left > window.innerWidth) {
    closeFloating(); return;
  }
  const margin = 12;
  const width = floatingEditor.offsetWidth, height = floatingEditor.offsetHeight;
  const left = Math.max(margin, Math.min(rect.left, window.innerWidth-width-margin));
  const below = rect.bottom+8;
  const top = below+height <= window.innerHeight-margin ? below : Math.max(margin, rect.top-height-8);
  floatingEditor.style.left = `${left}px`; floatingEditor.style.top = `${top}px`;
}
function openFloating(field, anchor) {
  if (!canEdit(field)) return;
  if (field.type === 'signature') {
    closeFloating(); highlightField(field);
    signatureEditor.open({label: field.label, value: values[field.key], anchor, onApply: value => updateValue(field, value)});
    return;
  }
  if (floating?.anchor === anchor) { floating.input.focus({preventScroll:true}); return; }
  closeFloating();
  highlightField(field);
  const title = document.createElement('label'); title.id = 'floating-field-title'; title.textContent = field.label;
  const input = makeInput(field); input.classList.add('floating-input'); title.htmlFor = input.id;
  const hint = document.createElement('span'); hint.className = 'floating-field-hint'; hint.textContent = field.required ? 'Required' : 'Optional';
  const error = document.createElement('p'); error.id = 'floating-field-error'; error.className = 'field-error'; error.textContent = errors[field.key] || '';
  input.setAttribute('aria-describedby', error.id);
  const done = document.createElement('button'); done.type = 'button'; done.textContent = 'Done';
  done.addEventListener('click', () => closeFloating(true));
  floatingEditor.replaceChildren(title, hint, input, error, done);
  floating = {field, anchor, input}; floatingEditor.hidden = false;
  anchor.setAttribute('aria-expanded', 'true');
  positionFloating(); input.focus({preventScroll:true});
}
function needsFloating(field, input) {
  if (field.type === 'textarea' || field.type === 'time' || allowsOther(field)) return true;
  if (!['select', 'date', 'time'].includes(field.type)) return false;
  const rect = input.getBoundingClientRect();
  return rect.height < 30 || rect.width < (field.type === 'date' ? 160 : 125);
}
document.addEventListener('pointerdown', event => {
  if (floating && !floatingEditor.contains(event.target) && event.target !== floating.anchor) closeFloating();
}, true);
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && floating) { event.preventDefault(); closeFloating(true); }
});
floatingEditor.addEventListener('keydown', event => {
  if (event.key === 'Enter' && event.target.tagName !== 'TEXTAREA' && event.target.tagName !== 'SELECT') {
    event.preventDefault(); closeFloating(true);
  }
});
floatingEditor.addEventListener('focusout', () => {
  requestAnimationFrame(() => {
    if (floating && !floatingEditor.contains(document.activeElement) && document.activeElement !== floating.anchor) closeFloating();
  });
});
window.addEventListener('resize', positionFloating);
document.addEventListener('scroll', positionFloating, true);
function focusField(key) {
  const control = controls.find(c => c.key === key);
  if (!control) return;
  if (snapshot.pages) showPage(control.page, false);
  control.input.scrollIntoView({behavior:'auto', block:'center'});
  const field = definition(key);
  openFloating(field, control.input);
}
function fitDocumentText(input) {
  if (!input.classList.contains('pdf-field')) return;
  requestAnimationFrame(()=>{
    const base=Number(input.dataset.baseFont || parseFloat(input.style.fontSize));
    input.style.fontSize=`${base}px`;
    if(input.clientWidth && input.scrollWidth>input.clientWidth) input.style.fontSize=`${Math.max(7,base*input.clientWidth/input.scrollWidth)}px`;
  });
}
function setRect(element, box) {
  const unit = 4/3; // Convert PDF points to CSS px at 96 dpi.
  element.style.left = `${box[0]*unit}px`; element.style.top = `${box[1]*unit}px`;
  element.style.width = `${box[2]*unit}px`; element.style.height = `${box[3]*unit}px`;
}
function drawPages() {
  $('document').classList.add('paginated-document');
  $('page-toolbar').hidden = !isReview; $('page-navigation').hidden = false;
  $('original-pdf').href = `${base}/${snapshot.version}/source.pdf`;
  for (const [index, page] of snapshot.pages.entries()) {
    const wrap = document.createElement('section'); wrap.className = 'pdf-page-wrap';
    wrap.setAttribute('aria-label', `Page ${index+1} of ${snapshot.pages.length}`);
    const stage = document.createElement('div'); stage.className = 'pdf-page';
    stage.style.width = `${page.width*4/3}px`; stage.style.height = `${page.height*4/3}px`;
    const image = document.createElement('img'); image.className = 'pdf-background'; image.src = page.image_url;
    image.alt = `Document page ${index+1}. A text transcript follows the document for screen readers.`;
    image.draggable = false;
    image.addEventListener('error', () => notice(`Page ${index+1} could not load. Refresh the preview to retry.`));
    stage.append(image);
    // Masks are restricted to actual placeholder glyph boxes; never move surrounding content.
    for (const placement of page.placements) for (const mask of placement.masks) {
      const cover = document.createElement('span'); cover.className = 'pdf-tag-mask'; cover.setAttribute('aria-hidden','true');
      setRect(cover, mask.box); cover.style.backgroundColor = mask.color; stage.append(cover);
    }
    for (const placement of page.placements) {
      const field = definition(placement.key);
      const input = makeInput(field, true);
      input.placeholder = field.role === 'vendor' ? 'Vendor' : ({initials:'Initials', signature:'Signature'})[field.type] || '';
      input.addEventListener('pointerdown', event => {
        if (needsFloating(field, input) && canEdit(field)) {
          event.preventDefault(); openFloating(field, input);
        }
      });
      input.addEventListener('focus', () => {
        if (restoringFocus) return;
        if (needsFloating(field, input)) openFloating(field, input);
        else { closeFloating(); highlightField(field); }
      });
      if (['select','date','time','textarea'].includes(field.type)) {
        input.setAttribute('aria-haspopup', 'dialog'); input.setAttribute('aria-expanded', 'false');
      }
      input.classList.add('pdf-field'); input.title = `${field.label} (${field.type})`;
      setRect(input, placement.box);
      input.style.setProperty('--field-line-height', `${Math.max(1, placement.box[3]*4/3-2)}px`);
      input.style.fontSize = `${Math.min(placement.font_size, placement.box[3]*.76)*4/3}px`;
      input.dataset.baseFont = parseFloat(input.style.fontSize);
      if (documentButton(field)) fitDocumentText(input);
      controls.push({key:field.key, input, page:index, button:documentButton(field)}); stage.append(input);
    }
    wrap.append(stage); $('document').append(wrap); pageViews.push({wrap, stage, page});
  }
  drawAdditionalVendorFields();
  new ResizeObserver(resizePages).observe($('document'));
  showPage(0, false);
}
function drawAdditionalVendorFields() {
  if (!isVendor) return;
  const unplaced = workflow.vendor_fields.filter(field => !controls.some(control => control.key === field.key));
  if (!unplaced.length) return;
  const panel = $('additional-vendor-fields');
  const heading = document.createElement('h2'); heading.textContent = 'Vendor signing';
  const hint = document.createElement('p'); hint.textContent = 'These fields will appear on the completion record attached to the final PDF.';
  panel.append(heading, hint);
  for (const field of unplaced) {
    const label = document.createElement('label'); label.textContent = field.label;
    const input = makeInput(field); label.htmlFor = input.id;
    panel.append(label, input);
    controls.push({key:field.key, input, page:snapshot.pages.length-1, button:false});
  }
}
function resizePages() {
  if (!snapshot.pages) return;
  const available = Math.max(250, $('document').clientWidth - 24);
  for (const {wrap, stage, page} of pageViews) {
    const width = page.width*4/3, height = page.height*4/3;
    const scale = $('zoom').value === 'fit' ? Math.min(1.5, available/width) : Number($('zoom').value);
    stage.style.transform = `scale(${scale})`;
    wrap.style.width = `${width*scale}px`; wrap.style.height = `${height*scale}px`;
  }
}
function showPage(index, scroll = true) {
  closeFloating();
  currentPage = Math.max(0, Math.min(index, snapshot.pages.length-1));
  for (const [i, view] of pageViews.entries()) view.wrap.hidden = i !== currentPage;
  const page = snapshot.pages[currentPage];
  $('page-label').textContent = `Page ${currentPage+1} of ${snapshot.pages.length}`;
  const lastPage = currentPage === snapshot.pages.length-1;
  $('next-page').hidden = lastPage;
  $('validate').hidden = !lastPage || locked;
  $('submit-confirmation').hidden = !lastPage || locked || isReview;
  $('additional-vendor-fields').hidden = !lastPage || !isVendor || !$('additional-vendor-fields').children.length;
  $('preview-disclaimer').hidden = !lastPage || !isReview;
  $('previous-page').disabled = currentPage === 0;
  $('next-page').disabled = currentPage === snapshot.pages.length-1;
  $('page-text').textContent = page.text;
  resizePages();
  for (const control of controls) if (control.page === currentPage && control.button) fitDocumentText(control.input);
  if (scroll) $('document').scrollIntoView({behavior:'smooth', block:'start'});
}
function drawLegacy() {
  $('document').innerHTML = snapshot.html;
  for (const marker of $('document').querySelectorAll('[data-field-key]')) {
    const field = definition(marker.dataset.fieldKey), input = makeInput(field, true);
    input.addEventListener('focus', () => highlightField(field));
    controls.push({key:field.key, input, page:0, button:documentButton(field)}); marker.replaceChildren(input);
  }
  $('page-navigation').hidden = false; $('previous-page').hidden = true; $('next-page').hidden = true;
  $('page-label').textContent = 'Document'; $('validate').hidden = false; $('preview-disclaimer').hidden = false;
  if (!isReview) $('validate').hidden = true;
  notice('This is an older HTML version. Render again from the workspace before submitting.');
}
function signingConfirmation(state) {
  return state.state === 'completed'
    ? {title:'The agreement has been completed.', description:'You may close this window now.'}
    : {title:'Your agreement has been submitted.', description:'It is awaiting the vendor’s signature. You may close this window now.'};
}
function updateSigningConfirmation(state) {
  const message = signingConfirmation(state);
  $('signing-success-title').textContent = message.title;
  $('signing-success-description').textContent = message.description;
}
function showConfirmation({title, description, warning='', kind='signing', tone='success'}) {
  const dialog = $('signing-success-dialog');
  if (!dialog) return;
  dialog.dataset.confirmationKind = kind;
  dialog.dataset.tone = tone;
  $('signing-success-title').textContent = title;
  $('signing-success-description').textContent = description;
  if ($('signing-success-icon')) $('signing-success-icon').textContent = tone === 'warning' ? '!' : tone === 'info' ? 'i' : '✓';
  $('signing-success-warning').textContent = warning;
  $('signing-success-warning').hidden = !warning;
  if (!dialog.open) dialog.showModal();
}
function showSigningConfirmation(state, warning) {
  if (isReview || state.superseded || !['awaiting_vendor','completed'].includes(state.state)) return;
  showConfirmation({...signingConfirmation(state), warning});
}
function sendingConfirmation(email) {
  if (email.delivery_status === 'sent') return {
    title:'The agreement has been sent to the client.',
    description:'You’ll be notified when the client signs. You may close this window now.',
    tone:'success',
  };
  if (email.delivery_status === 'local') return {
    title:'Test email created.',
    description:'No email was sent. The invitation is available in the local email outbox.',
    tone:'info',
  };
  return {
    title:'Email delivery needs attention.',
    description:'The invitation has been saved, but email delivery has not been confirmed. Check the email outbox before retrying.',
    warning:email.delivery_error || '', tone:'warning',
  };
}
$('signing-success-close')?.addEventListener('click', () => $('signing-success-dialog').close());
function applyWorkflow(state) {
  workflow = state; locked = isLocked(state);
  $('request-corrections').hidden = !isVendor || locked || state.state !== 'awaiting_vendor';
  $('review-send').hidden = !isReview || locked || !snapshot.pages;
  $('rerender').disabled = locked;
  if (!locked) return;
  closeFloating();
  if (state.client) { for (const [key,value] of Object.entries(state.client.values)) { const field=definition(key); if(field) updateValue(field,value); } dirty=false; }
  if (state.vendor) { for (const [key,value] of Object.entries(state.vendor.values)) { const field=definition(key); if(field) updateValue(field,value); } dirty=false; }
  $('validate').hidden = true; $('submit-confirmation').hidden = true;
  for (const control of controls) control.input.disabled = true;
  $('submission-receipt').hidden = false;
  $('receipt-title').hidden = false;
  if (isVendor && state.state === 'editable' && !state.superseded) {
    $('receipt-title').textContent = 'Awaiting client submission';
    $('receipt-description').textContent = 'The client must submit their answers and signature before you can countersign.';
    $('receipt-download').hidden = true; return;
  }
  if (isReview && state.sent && state.state === 'editable' && !state.superseded) {
    $('receipt-title').textContent = 'Awaiting client';
    $('receipt-description').textContent = 'This version is locked. The invitation and its delivery status are available in the workspace email outbox.';
    $('receipt-download').hidden = true; return;
  }
  const completed = state.state === 'completed';
  const confirmation = signingConfirmation(state);
  $('receipt-title').textContent = state.superseded ? 'This version has been superseded' : confirmation.title;
  $('receipt-description').textContent = state.superseded ? 'A newer revision exists. This version remains saved for your records.' : completed ? confirmation.description : `${confirmation.title} ${confirmation.description}`;
  const awaitingVendor = !completed && !state.superseded;
  $('receipt-title').hidden = awaitingVendor;
  const link = state.final_pdf_url || state.client_pdf_url;
  $('receipt-download').hidden = awaitingVendor || !link;
  if (link) $('receipt-download').href = link;
  if ($('signing-success-dialog')?.open && $('signing-success-dialog').dataset.confirmationKind === 'signing' && !state.superseded) updateSigningConfirmation(state);
}
function drawDocument() {
  if (snapshot.pages) drawPages(); else drawLegacy();
  if (!isReview) return;
  $('field-count').textContent = `${snapshot.fields.length} unique fields · ${snapshot.occurrences} placements`;
  for (const field of snapshot.fields) {
    const row = document.createElement('div'); row.className = 'field-summary';
    const jump = document.createElement('button'); jump.type = 'button'; jump.textContent = field.label;
    jump.addEventListener('click', () => focusField(field.key));
    const detail = document.createElement('small');
    const pages = [...new Set(controls.filter(c => c.key === field.key).map(c => c.page+1))];
    detail.textContent = `${field.type} · ${field.role} · ${field.required ? 'required' : 'optional'}${snapshot.pages ? ' · Page '+pages.join(', ') : ''}`;
    row.append(jump, detail); $('field-list').append(row);
  }
}
$('previous-page').addEventListener('click', () => showPage(currentPage-1));
$('next-page').addEventListener('click', () => showPage(currentPage+1));
$('zoom').addEventListener('change', () => { closeFloating(); resizePages(); });
$('preview-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (snapshot.pages && currentPage < snapshot.pages.length-1) { showPage(currentPage+1); return; }
  if (locked) return;
  if (!isReview && !$('signing-consent').checked) { notice('Confirm your agreement before submitting.'); $('submit-confirmation').scrollIntoView({block:'center'}); return; }
  $('validate').disabled = true;
  const payload = Object.create(null);
  for (const field of signingFields()) payload[field.key] = values[field.key] ?? (field.type === 'checkbox' ? false : '');
  try {
    if (!isVendor) {
      const result = await api(`${base}/${snapshot.version}/validate`, post(payload));
      showErrors(result.errors);
      if (!result.valid) return;
    }
    if (isReview) { notice('All fields look good. This review check does not submit the agreement.'); return; }
    const state = await api(`${base}/${snapshot.version}/${isVendor ? 'countersign' : 'submit'}`, post({values:payload, consent:$('signing-consent').checked}));
    const warning = [state.drive_archive?.error, state.email_delivery?.error ? `Agreement signed and saved. Completion email: ${state.email_delivery.error} Check the workspace outbox.` : ''].filter(Boolean).join(' ');
    closeFloating(); applyWorkflow(state); notice(warning); dirty = false;
    $('submission-receipt').scrollIntoView({behavior:'smooth', block:'center'});
    showSigningConfirmation(state, warning);
  } catch(error) { notice(error.message); if (error.fields) showErrors(error.fields); }
  finally { $('validate').disabled = false; }
});
$('version').addEventListener('change', () => {
  if (dirty && !confirm('Switch versions? Temporary preview answers will be cleared.')) { $('version').value = String(snapshot.version); return; }
  location.search = `?version=${$('version').value}&review=1`;
});
$('rerender').addEventListener('click', async () => {
  if (dirty && !confirm('Render again? Temporary preview answers will be cleared. The current document snapshot will remain in version history.')) return;
  $('rerender').disabled = true; notice('Exporting Google Docs pages and locating fields…');
  try { const result = await api(base, post()); location.href = result.preview_url + (isReview ? '&review=1' : ''); }
  catch(error) { notice(error.message); $('rerender').disabled = false; }
});
(async () => {
  try {
    const versions = await api(base);
    if (!versions.length) throw new Error('No saved render yet. Return to the workspace and click Render Agreement.');
    const requested = new URLSearchParams(location.search).get('version') || versions[0].version;
    snapshot = await api(`${base}/${encodeURIComponent(requested)}`);
    workflow = await api(`${base}/${snapshot.version}/workflow`);
    locked = isLocked(workflow);
    for (const field of snapshot.fields) {
      if (field.role === activeRole && !['signature','initials'].includes(field.type) && !isSigningDate(field) && Object.hasOwn(field,'default')) values[field.key] = field.default;
      if (field.role === 'client' && !['signature','initials'].includes(field.type) && !isSigningDate(field) && Object.hasOwn(snapshot.prefill_values || {},field.key)) values[field.key] = snapshot.prefill_values[field.key];
    }
    $('correction-note-panel').hidden = !snapshot.correction_note;
    $('correction-note-text').textContent = snapshot.correction_note || '';
    for (const field of snapshot.fields) if (isSigningDate(field) && field.role === activeRole) values[field.key] = workflow.signing_date;
    if (workflow.client) Object.assign(values, workflow.client.values);
    if (workflow.vendor) Object.assign(values, workflow.vendor.values);
    $('consent-text').textContent = isVendor ? workflow.vendor_consent : workflow.client_consent;
    if (isReview) $('document-instructions').textContent = 'Review each page, then choose Review & send when the agreement is ready for the client.';
    if (isVendor) {
      document.body.classList.add('vendor-document-view');
      $('document-instructions').textContent = 'Review the client’s completed answers, then fill in your vendor fields and sign on the last page.';
      $('vendor-workspace').hidden = false;
    }
    $('validate').textContent = isReview ? 'Check fields' : isVendor ? 'Sign & finalize' : 'Submit agreement';
    $('preview-title').textContent = snapshot.name; document.title = `${snapshot.name} · ${isVendor ? 'Vendor signing' : isReview ? 'Review' : 'Agreement'}`;
    $('review-details').hidden = !isReview;
    $('snapshot-meta').textContent = `Version ${snapshot.version} · Rendered ${new Date(snapshot.created_at).toLocaleString()}${snapshot.pages ? ' · '+snapshot.pages.length+' pages' : ''}${snapshot.mode === 'demo' ? ' · Demo document' : ''}`;
    for (const item of versions) $('version').append(new Option(`Version ${item.version} — ${new Date(item.created_at).toLocaleString()}`, item.version));
    $('version').value = String(snapshot.version); $('version').disabled = false;
    if (snapshot.document_url) { $('edit-doc').href = snapshot.document_url; $('edit-doc').hidden = false; }
    for (const warning of snapshot.warnings) { const item = document.createElement('li'); item.textContent = warning; $('warnings').append(item); }
    drawDocument(); applyWorkflow(workflow); $('validate').disabled = false; $('rerender').disabled = locked;
    setInterval(async () => {
      if (workflow?.state === 'awaiting_vendor') {
        try { applyWorkflow(await api(`${base}/${snapshot.version}/workflow`)); } catch {}
      }
    }, 15000);
    if (snapshot.mode === 'demo') $('rerender').textContent = 'Render demo again';
  } catch(error) { notice(error.message); $('preview-title').textContent = 'Preview unavailable'; }
})();

let sending = false;
let recipientDefaultsVersion = null;
function recipientDefault(key) {
  // Use the saved document, not temporary answers entered while reviewing it.
  const value = snapshot.fields.find(field => field.key === key)?.default;
  if (typeof value !== 'string') return '';
  const text = value.trim();
  if (!text || text.toUpperCase() === 'REQUIRED') return '';
  if (key === 'client.provided_email' && (text.length > 254 || !/^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/.test(text))) return '';
  if (key === 'client.provided_name' && text.length > 200) return '';
  return text;
}
$('review-send').addEventListener('click', async () => {
  if (!isReview || locked || sending) return;
  closeFloating();
  if (recipientDefaultsVersion !== snapshot.version) {
    $('recipient-name').value = recipientDefault('client.provided_name');
    $('recipient-email').value = recipientDefault('client.provided_email');
    recipientDefaultsVersion = snapshot.version;
  }
  $('send-contract-name').textContent = `${snapshot.name} · Version ${snapshot.version}`;
  $('send-reviewed').checked = false; $('send-error').textContent = '';
  try { await window.emailComposer?.load(snapshot.name); $('send-dialog').showModal(); }
  catch(error) { notice(error.message); }
});
$('send-cancel').addEventListener('click', () => $('send-dialog').close());
$('send-dialog').addEventListener('cancel', event => { if (sending) event.preventDefault(); });
$('send-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!isReview || locked || sending || !$('send-form').reportValidity()) return;
  sending = true; $('send-confirm').disabled = true; $('send-cancel').disabled = true;
  try {
    const email = await api(`${base}/${snapshot.version}/send`, post({recipient_name:$('recipient-name').value,recipient_email:$('recipient-email').value,reviewed:$('send-reviewed').checked,email_options:window.emailComposer?.values()}));
    // Lock immediately after success even if a later refresh of the workflow fails.
    applyWorkflow({...workflow, sent:true}); dirty = false;
    $('send-dialog').close(); $('send-result').hidden = false;
    $('sent-client-link').href = email.link;
    const confirmation = sendingConfirmation(email);
    $('send-result-title').textContent = confirmation.title;
    $('send-result-description').textContent = [confirmation.description,confirmation.warning].filter(Boolean).join(' ');
    $('send-result').scrollIntoView({behavior:'smooth',block:'center'});
    showConfirmation({...confirmation, kind:'sending'});
  } catch(error) { $('send-error').textContent = error.message; }
  finally { sending = false; $('send-confirm').disabled = false; $('send-cancel').disabled = false; }
});

let requestingCorrections = false;
$('request-corrections').addEventListener('click', async () => {
  if (!isVendor || locked || requestingCorrections) return;
  closeFloating(); $('corrections-error').textContent = '';
  try {
    const delivery = await api('/api/email-delivery');
    $('corrections-confirm').textContent = delivery.mode === 'gmail' ? 'Send correction request' : 'Create test request';
    $('corrections-mode').textContent = delivery.mode === 'gmail' ? 'This sends an email through Gmail to the same client email address. The signing-link expiration starts again.' : 'Local test: the request will appear in the email outbox. No email will be sent.';
    $('corrections-dialog').showModal();
  } catch(error) { notice(error.message); }
});
$('corrections-cancel').addEventListener('click', () => $('corrections-dialog').close());
$('corrections-dialog').addEventListener('cancel', event => { if (requestingCorrections) event.preventDefault(); });
$('corrections-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!isVendor || locked || requestingCorrections || !$('corrections-form').reportValidity()) return;
  requestingCorrections = true; $('corrections-confirm').disabled = true; $('corrections-cancel').disabled = true; $('validate').disabled = true;
  try {
    const result = await api(`${base}/${snapshot.version}/request-corrections`,post({note:$('corrections-note').value}));
    applyWorkflow({...workflow,superseded:true}); dirty = false;
    $('corrections-dialog').close(); $('send-result').hidden = false;
    const email = result.email;
    $('sent-client-link').href = email.link;
    $('send-result-title').textContent = email.delivery_status === 'sent' ? 'Correction request sent' : email.delivery_status === 'local' ? 'Test correction request created' : 'Correction email needs attention';
    $('send-result-description').textContent = email.delivery_status === 'sent' ? 'The client has a new link with their previous answers filled in. You will be notified when they sign again.' : email.delivery_error || 'The new signing link and request are saved in the email outbox. No email was sent.';
    $('send-result').scrollIntoView({behavior:'smooth',block:'center'});
  } catch(error) { $('corrections-error').textContent = error.message; }
  finally { requestingCorrections = false; $('corrections-confirm').disabled = false; $('corrections-cancel').disabled = false; $('validate').disabled = false; }
});
