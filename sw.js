const shell = 'stock-intelligence.shell.v4.1';
const mascot = new URL('./mascots.webp', self.registration.scope).href;
const manifest = new URL('./manifest.webmanifest', self.registration.scope).href;
const home = new URL('./index.html', self.registration.scope).href;
self.addEventListener('install', event => {
 event.waitUntil(caches.open(shell).then(cache=>cache.addAll([home,mascot,manifest].map(url=>new Request(url,{cache:'reload'})))).then(()=>self.skipWaiting()));
});
self.addEventListener('activate',event=>{
 event.waitUntil(caches.keys().then(keys=>Promise.all(keys
  .filter(key=>key.startsWith('stock-intelligence.shell.v')&&key!==shell)
  .map(key=>caches.delete(key)))).then(()=>self.clients.claim()));
});
self.addEventListener('fetch',event=>{
 const request=event.request;
 if(request.url===mascot||request.url===manifest){event.respondWith(caches.match(request,{cacheName:shell}).then(response=>response??fetch(request)));return;}
 const url=new URL(request.url);
 if(request.mode!=='navigate'||!url.href.startsWith(self.registration.scope)||
    !['', 'index.html'].includes(url.pathname.slice(new URL(self.registration.scope).pathname.length)))return;
 event.respondWith(fetch(request,{cache:'no-store'}).then(async response=>{
  if(response.ok)await (await caches.open(shell)).put(home,response.clone());
  return response;
 }).catch(()=>caches.match(home,{cacheName:shell})));
});
