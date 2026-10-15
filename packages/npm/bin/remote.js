#!/usr/bin/env node
'use strict';
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawnSync}=require('node:child_process');
const version=require('../package.json').version;
const platform={linux:'linux',darwin:'macos',win32:'windows'}[process.platform];
const arch={x64:'amd64',arm64:'arm64'}[process.arch];
if(!platform||!arch||(platform==='windows'&&arch!=='amd64')){console.error('Unsupported platform');process.exit(2);}
const ext=platform==='windows'?'.exe':'';
const dest=path.join(os.homedir(),'.livingruntime','bin','livingruntime-remote-connector'+ext);
const url='https://github.com/jboone1989/livingruntime-remote/releases/download/v'+version+'/livingruntime-remote-connector-'+platform+'-'+arch+ext;
async function main(){
 const check=fs.existsSync(dest)&&spawnSync(dest,['--version'],{encoding:'utf8'});
 if(!check||check.status!==0||check.stdout.trim()!==version){
  fs.mkdirSync(path.dirname(dest),{recursive:true,mode:0o700});
  const response=await fetch(url,{signal:AbortSignal.timeout(180000)});
  if(!response.ok)throw Error('Release download HTTP '+response.status);
  const bytes=Buffer.from(await response.arrayBuffer());
  if(bytes.length<100000)throw Error('Unexpectedly small download');
  const temp=dest+'.tmp-'+process.pid;
  try{
   fs.writeFileSync(temp,bytes,{mode:0o700});
   if(process.platform!=='win32')fs.chmodSync(temp,0o700);
   const verify=spawnSync(temp,['--version'],{encoding:'utf8'});
   if(verify.status!==0||verify.stdout.trim()!==version)throw Error('Executable version mismatch');
   fs.renameSync(temp,dest);
  }finally{try{fs.unlinkSync(temp)}catch(_){}}
 }
 const r=spawnSync(dest,process.argv.slice(2),{stdio:'inherit'});
 if(r.error)throw r.error;
 process.exit(r.status??1);
}
main().catch(e=>{console.error(e.message);process.exit(1)});
