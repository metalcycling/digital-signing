/* Signature capture stays in browser memory until the contract submission workflow exists. */
(() => {
  const MAX_UPLOAD = 5 * 1024 * 1024;
  const WIDTH = 1000, HEIGHT = 260;
  function canvas() { const c = document.createElement('canvas'); c.width = WIDTH; c.height = HEIGHT; return c; }
  function inkImage(source) {
    const ctx = source.getContext('2d', {willReadFrequently:true});
    const {width, height} = source;
    const data = ctx.getImageData(0,0,width,height).data;
    let left=width, top=height, right=-1, bottom=-1, count=0;
    for (let y=0;y<height;y++) for (let x=0;x<width;x++) {
      const i=(y*width+x)*4;
      if (data[i+3]>24 && Math.min(data[i],data[i+1],data[i+2])<240) {
        left=Math.min(left,x); right=Math.max(right,x); top=Math.min(top,y); bottom=Math.max(bottom,y); count++;
      }
    }
    if (count<12 || right-left<2 || bottom-top<2) throw new Error('Add a visible signature before applying it.');
    const margin=8;
    left=Math.max(0,left-margin); top=Math.max(0,top-margin);
    right=Math.min(width-1,right+margin); bottom=Math.min(height-1,bottom+margin);
    const output=document.createElement('canvas'); output.width=right-left+1; output.height=bottom-top+1;
    output.getContext('2d').drawImage(source,left,top,output.width,output.height,0,0,output.width,output.height);
    return output.toDataURL('image/png');
  }
  function loadImage(url) {
    return new Promise((resolve,reject) => {
      const image=new Image(); image.onload=()=>resolve(image);
      image.onerror=()=>reject(new Error('This image could not be read. Choose a PNG, JPEG, or WebP image.')); image.src=url;
    });
  }
  class SignatureEditor {
    constructor() {
      this.dialog=document.createElement('dialog'); this.dialog.className='signature-dialog';
      this.dialog.setAttribute('aria-labelledby','signature-title');
      this.dialog.innerHTML=`
        <div class="signature-heading"><div><h2 id="signature-title">Add your signature</h2><p>Choose how you’d like to sign.</p></div><button type="button" class="signature-close" aria-label="Close signature editor">×</button></div>
        <div class="signature-tabs" role="tablist" aria-label="Signature method">
          <button type="button" id="signature-tab-type" role="tab" data-method="typed" aria-controls="signature-panel-typed">Type</button>
          <button type="button" id="signature-tab-draw" role="tab" data-method="drawn" aria-controls="signature-panel-drawn">Draw</button>
          <button type="button" id="signature-tab-upload" role="tab" data-method="uploaded" aria-controls="signature-panel-uploaded">Upload</button>
        </div>
        <section id="signature-panel-typed" role="tabpanel" aria-labelledby="signature-tab-type"><label for="signature-name">Your name</label><input id="signature-name" maxlength="100" autocomplete="name" placeholder="Type your name"><div class="signature-pad"><canvas id="signature-typed" width="1000" height="260" aria-label="Cursive signature preview"></canvas></div></section>
        <section id="signature-panel-drawn" role="tabpanel" aria-labelledby="signature-tab-draw"><p class="signature-instruction">Draw with your mouse, trackpad, or finger.</p><div class="signature-pad"><canvas id="signature-drawn" width="1000" height="260" aria-label="Draw your signature. Alternatively use Type or Upload."></canvas></div><div class="signature-draw-tools"><button type="button" id="signature-undo" class="text-button">Undo</button><button type="button" id="signature-clear" class="text-button">Clear</button></div></section>
        <section id="signature-panel-uploaded" role="tabpanel" aria-labelledby="signature-tab-upload"><label for="signature-file">Choose a signature image</label><input id="signature-file" type="file" accept="image/png,image/jpeg,image/webp"><p class="signature-instruction">PNG, JPEG, or WebP · up to 5 MB. A tightly cropped signature works best.</p><div class="signature-pad signature-upload-preview"><img id="signature-uploaded-image" alt="Uploaded signature preview" hidden><span id="signature-upload-placeholder">Your signature will appear here</span></div></section>
        <p id="signature-error" class="field-error" role="status" aria-live="polite"></p>
        <p class="signature-preview-note">Your signature will be saved when you submit the agreement.</p>
        <div class="signature-actions"><button type="button" id="signature-remove" class="text-button" hidden>Remove signature</button><button type="button" id="signature-cancel" class="text-button">Cancel</button><button type="button" id="signature-apply">Apply signature</button></div>`;
      document.body.append(this.dialog);
      this.el=id=>this.dialog.querySelector('#'+id);
      this.tabs=[...this.dialog.querySelectorAll('[role=tab]')];
      this.tabs.forEach((tab,index)=> {
        tab.addEventListener('click',()=>this.select(tab.dataset.method));
        tab.addEventListener('keydown',event=> {
          let target;
          if(event.key==='ArrowRight') target=(index+1)%3;
          if(event.key==='ArrowLeft') target=(index+2)%3;
          if(event.key==='Home') target=0;
          if(event.key==='End') target=2;
          if(target!==undefined) {event.preventDefault(); this.select(this.tabs[target].dataset.method); this.tabs[target].focus();}
        });
      });
      this.dialog.querySelector('.signature-close').addEventListener('click',()=>this.close());
      this.el('signature-cancel').addEventListener('click',()=>this.close());
      this.dialog.addEventListener('close',()=> { this.session++; this.anchor?.focus({preventScroll:true}); });
      this.el('signature-remove').addEventListener('click',()=> { this.onApply(null); this.close(); });
      this.el('signature-name').addEventListener('input',()=>this.paintTyped());
      this.el('signature-file').addEventListener('change',()=>this.upload());
      this.el('signature-apply').addEventListener('click',()=>this.apply());
      this.el('signature-undo').addEventListener('click',()=> { this.strokes.pop(); this.paintDrawn(); this.error(''); });
      this.el('signature-clear').addEventListener('click',()=> { this.strokes=[]; this.drawBase=null; this.paintDrawn(); this.error(''); });
      this.session=0; this.typedRevision=0; this.uploadRevision=0;
      const pad=this.el('signature-drawn');
      const point=event=> { const box=pad.getBoundingClientRect(); return {x:Math.max(0,Math.min(WIDTH,(event.clientX-box.left)*WIDTH/box.width)), y:Math.max(0,Math.min(HEIGHT,(event.clientY-box.top)*HEIGHT/box.height))}; };
      pad.addEventListener('pointerdown',event=> {
        if(event.button!==0 || this.pointer!==null) return;
        event.preventDefault(); this.pointer=event.pointerId; pad.setPointerCapture(event.pointerId);
        this.strokes.push([point(event)]); this.error(''); this.paintDrawn();
      });
      pad.addEventListener('pointermove',event=> {
        if(event.pointerId!==this.pointer) return;
        const samples=event.getCoalescedEvents?.();
        for(const sample of samples?.length ? samples : [event]) this.strokes[this.strokes.length-1].push(point(sample));
        this.paintDrawn();
      });
      const end=event=> { if(event.pointerId===this.pointer) this.pointer=null; };
      pad.addEventListener('pointerup',end); pad.addEventListener('pointercancel',end); pad.addEventListener('lostpointercapture',end);
    }
    error(message) { this.el('signature-error').textContent=message; }
    select(method) {
      this.method=method;
      for(const tab of this.tabs) { const selected=tab.dataset.method===method; tab.setAttribute('aria-selected',String(selected)); tab.tabIndex=selected?0:-1; }
      for(const kind of ['typed','drawn','uploaded']) this.el('signature-panel-'+kind).hidden=kind!==method;
      this.error(''); this.el('signature-apply').disabled=this.busy;
    }
    async open({label,value,onApply,anchor}) {
      this.onApply=onApply; this.anchor=anchor; this.session++; const session=this.session;
      this.busy=false; this.pointer=null; this.strokes=[]; this.drawBase=null; this.uploadImage=null;
      this.uploadRevision++; this.typedRevision++;
      this.el('signature-title').textContent=label==='Signature' ? 'Add your signature' : label;
      this.el('signature-name').value=value?.method==='typed' ? value.typed_name : '';
      this.el('signature-file').value='';
      this.el('signature-remove').hidden=!value;
      this.el('signature-uploaded-image').hidden=true; this.el('signature-uploaded-image').removeAttribute('src');
      this.el('signature-upload-placeholder').hidden=false;
      this.paintDrawn(); this.select(value?.method || 'typed'); this.dialog.showModal();
      if(this.method==='typed') this.el('signature-name').focus();
      else this.tabs.find(tab=>tab.dataset.method===this.method).focus();
      this.paintTyped();
      if(value?.image && value.method!=='typed') {
        this.busy=true; this.el('signature-apply').disabled=true;
        try {
          const img=await loadImage(value.image);
          if(session!==this.session) return;
          if(value.method==='drawn') {this.drawBase=img; this.paintDrawn();}
          else {this.uploadImage=value.image; this.showUpload(value.image);}
        } catch(error) {if(session===this.session) this.error(error.message);}
        finally {if(session===this.session) {this.busy=false; this.el('signature-apply').disabled=false;}}
      }
    }
    close() { this.dialog.close(); }
    async paintTyped() {
      const revision=++this.typedRevision, session=this.session;
      const target=this.el('signature-typed'); target.getContext('2d').clearRect(0,0,WIDTH,HEIGHT);
      const name=this.el('signature-name').value.trim(); if(!name) return;
      try {
        const fonts=await document.fonts.load('90px BelloSignature');
        if(!fonts.length) throw new Error('The signature font could not load. Refresh, or use Draw or Upload.');
        if(revision!==this.typedRevision || session!==this.session) return;
        const ctx=target.getContext('2d'); ctx.clearRect(0,0,WIDTH,HEIGHT);
        let size=105; ctx.font=`${size}px BelloSignature`;
        if(ctx.measureText(name).width>WIDTH-70) size*= (WIDTH-70)/ctx.measureText(name).width;
        ctx.font=`${size}px BelloSignature`; ctx.fillStyle='#172d39'; ctx.textAlign='center'; ctx.textBaseline='middle';
        ctx.fillText(name,WIDTH/2,HEIGHT/2);
        if(this.method==='typed') this.error('');
      } catch(error) {if(session===this.session && this.method==='typed') this.error(error.message);}
    }
    paintDrawn() {
      const ctx=this.el('signature-drawn').getContext('2d'); ctx.clearRect(0,0,WIDTH,HEIGHT);
      if(this.drawBase) {
        const scale=Math.min((WIDTH-30)/this.drawBase.width,(HEIGHT-30)/this.drawBase.height);
        const w=this.drawBase.width*scale,h=this.drawBase.height*scale;
        ctx.drawImage(this.drawBase,(WIDTH-w)/2,(HEIGHT-h)/2,w,h);
      }
      ctx.strokeStyle='#172d39'; ctx.fillStyle='#172d39'; ctx.lineWidth=3.5; ctx.lineCap='round'; ctx.lineJoin='round';
      for(const stroke of this.strokes) {
        if(stroke.length===1) {ctx.beginPath();ctx.arc(stroke[0].x,stroke[0].y,1.75,0,Math.PI*2);ctx.fill();continue;}
        ctx.beginPath();ctx.moveTo(stroke[0].x,stroke[0].y);for(const p of stroke.slice(1)) ctx.lineTo(p.x,p.y);ctx.stroke();
      }
    }
    showUpload(url) {this.el('signature-uploaded-image').src=url;this.el('signature-uploaded-image').hidden=false;this.el('signature-upload-placeholder').hidden=true;}
    async upload() {
      const file=this.el('signature-file').files[0]; if(!file) return;
      const session=this.session, revision=++this.uploadRevision;
      this.uploadImage=null; this.el('signature-uploaded-image').hidden=true; this.el('signature-upload-placeholder').hidden=false;
      this.busy=true; this.el('signature-apply').disabled=true; this.error('');
      let url;
      try {
        if(!['image/png','image/jpeg','image/webp'].includes(file.type) || file.size>MAX_UPLOAD) throw new Error('Choose a PNG, JPEG, or WebP image no larger than 5 MB.');
        url=URL.createObjectURL(file); const img=await loadImage(url);
        if(session!==this.session || revision!==this.uploadRevision) return;
        if(img.naturalWidth*img.naturalHeight>12000000) throw new Error('Choose an image smaller than 12 megapixels.');
        const target=canvas(), ctx=target.getContext('2d');
        const scale=Math.min(WIDTH/img.naturalWidth,HEIGHT/img.naturalHeight);
        ctx.drawImage(img,0,0,img.naturalWidth*scale,img.naturalHeight*scale);
        this.uploadImage=inkImage(target); this.showUpload(this.uploadImage);
      } catch(error) {if(session===this.session && revision===this.uploadRevision) this.error(error.message);}
      finally {if(url) URL.revokeObjectURL(url);if(session===this.session && revision===this.uploadRevision) {this.busy=false;this.el('signature-apply').disabled=false;}}
    }
    async apply() {
      if(this.busy) return;
      const session=this.session, method=this.method;
      this.el('signature-apply').disabled=true;
      try {
        let image, typedName;
        if(method==='typed') {
          typedName=this.el('signature-name').value.trim();
          if(!typedName || typedName.length>100) throw new Error('Type your name first (up to 100 characters).');
          await this.paintTyped();
          if(session!==this.session || method!==this.method) return;
          if(typedName!==this.el('signature-name').value.trim()) throw new Error('Your name changed. Apply the updated signature again.');
          image=inkImage(this.el('signature-typed'));
        } else if(method==='drawn') image=inkImage(this.el('signature-drawn'));
        else {image=this.uploadImage;if(!image) throw new Error('Choose a signature image first.');}
        const signature={method,image}; if(typedName) signature.typed_name=typedName;
        this.onApply(signature); this.close();
      } catch(error) {if(session===this.session) this.error(error.message);}
      finally {if(session===this.session) this.el('signature-apply').disabled=false;}
    }
  }
  window.SignatureEditor=SignatureEditor;
})();
