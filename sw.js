const shell = 'stock-intelligence.shell.v3.18.1';
const home = new URL('./index.html', self.registration.scope).href;
self.addEventListener('install', event => {
 event.waitUntil(caches.open(shell).then(cache=>cache.add(new Request(home,{cache:'reload'}))).then(()=>self.skipWaiting()));
});
self.addEventListener('activate',event=>{
 event.waitUntil(self.clients.claim());
});
self.addEventListener('fetch',event=>{
 const request=event.request;
 const url=new URL(request.url);
 if(request.mode!=='navigate'||!url.href.startsWith(self.registration.scope)||
    !['', 'index.html'].includes(url.pathname.slice(new URL(self.registration.scope).pathname.length)))return;
 event.respondWith(fetch(request,{cache:'no-store'}).then(async response=>{
  if(response.ok)await (await caches.open(shell)).put(home,response.clone());
  return response;
 }).catch(()=>caches.match(home,{cacheName:shell})));
});
