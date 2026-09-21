<script setup lang="ts">
import {ref,watch} from 'vue';
const props=defineProps<{conversationId:string;modelValue:string[]}>();
const emit=defineEmits<{'update:modelValue':[string[]]}>();
const files=ref<{id:string;name:string}[]>([]),error=ref(''),busy=ref(false);
watch(()=>props.conversationId,()=>{files.value=[];emit('update:modelValue',[]);});
watch(()=>props.modelValue,ids=>{files.value=files.value.filter(f=>ids.includes(f.id));});
async function upload(event:Event){
 const input=event.target as HTMLInputElement,cid=props.conversationId;
 const selected=[...(input.files||[])];input.value='';error.value='';
 if(files.value.length+selected.length>4){error.value='每条消息最多四个附件。';return;}
 busy.value=true;
 try{for(const file of selected){
  if(file.size>8*1024*1024)throw new Error('单个附件不能超过8MB。');
  const response=await fetch(`/api/attachments?conversationId=${encodeURIComponent(cid)}&name=${encodeURIComponent(file.name)}`,{method:'POST',body:file});
  const data=await response.json();if(!response.ok)throw new Error(data.detail);
  if(cid!==props.conversationId)break;
  files.value.push(data);emit('update:modelValue',files.value.map(f=>f.id));
 }}catch(e){error.value=(e as Error).message;}finally{busy.value=false;}
}
function remove(id:string){files.value=files.value.filter(f=>f.id!==id);emit('update:modelValue',files.value.map(f=>f.id));}
</script>
<template><div class="attachments"><label>{{busy?'上传中…':'＋ 图片 / 文件'}}<input aria-label="上传图片或文件" type="file" multiple :disabled="busy" accept=".png,.jpg,.jpeg,.webp,.pdf,.docx,.xlsx,.txt,.md,.csv,.py,.json" @change="upload"></label><button v-for="file in files" :key="file.id" type="button" class="text-button" @click="remove(file.id)">{{file.name}} ×</button><span v-if="error" role="alert">{{error}}</span></div></template>
<style scoped>.attachments{font-size:12px;padding:4px 0;color:#628ca2}.attachments input{position:absolute;width:1px;height:1px;opacity:0}.attachments label{cursor:pointer}.attachments label:focus-within{outline:2px solid #65c5dd;outline-offset:3px}</style>
