// Client signing pages never request staff identity or show staff controls.
(async () => {
  if(location.pathname.startsWith('/sign/'))return;
  try {
    const response=await fetch('/auth/status');
    if(!response.ok)return;
    const state=await response.json();
    if(state.mode!=='google')return;
    if(!state.user){location.assign('/login?'+new URLSearchParams({next:location.pathname+location.search}));return;}
    const host=document.querySelector('header');if(!host)return;
    const account=document.createElement('div');account.className='staff-account';
    const name=document.createElement('span');name.textContent=state.user.email;
    const button=document.createElement('button');button.type='button';button.className='text-button';button.textContent='Sign out';
    button.addEventListener('click',async()=>{
      button.disabled=true;
      try {
        const result=await fetch('/auth/logout',{method:'POST',headers:{'X-Workspace-Request':'1'}});
        if(!result.ok)throw new Error();
        location.assign('/login');
      }catch{button.disabled=false;button.textContent='Retry sign out';}
    });
    account.append(name,button);
    if(host.classList.contains('client-heading'))host.prepend(account);
    else host.append(account);
  }catch{}
})();
