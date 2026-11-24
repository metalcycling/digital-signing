(async () => {
  const message=document.getElementById('login-message'), button=document.getElementById('google-login');
  const params=new URLSearchParams(location.search);
  const errors={denied:'This Google account is not approved for this workspace.',expired:'Your sign-in attempt expired. Please try again.',cancelled:'Sign-in was cancelled. You can try again.',invalid:'We couldn’t complete Google sign-in. Please try again.',setup:'Staff sign-in is not configured yet. Please contact the workspace owner.',approval:'This Google account needs one-time approval. Send this code to the workspace owner, then sign in again after approval:'};
  message.textContent=errors[params.get('error')] || '';
  if(params.get('error')==='approval' && /^[A-F0-9]{12}$/.test(params.get('code') || '')) message.textContent+='\n'+params.get('code');
  try {
    const response=await fetch('/auth/status');
    if(!response.ok) throw new Error();
    const state=await response.json();
    if(state.mode==='local'){location.replace('/');return;}
    if(!state.configured){message.textContent=errors.setup;return;}
    button.href='/auth/start?'+new URLSearchParams({next:params.get('next') || '/'});
    button.hidden=false;
  } catch {message.textContent='Unable to check sign-in settings. Please refresh and try again.';}
})();
