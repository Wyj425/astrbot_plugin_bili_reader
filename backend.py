import asyncio, json, re, time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, urlencode
from zoneinfo import ZoneInfo
import aiohttp
from astrbot.api import logger
TZ = ZoneInfo('Asia/Shanghai')
def clean(text, limit=4000):
    import html
    return re.sub(r'<[^>]*>', '', html.unescape(str(text or '')))[:limit].strip()
async def bounded_json(response, limit=3*1024*1024):
    body=bytearray()
    async for chunk in response.content.iter_chunked(65536):
        body.extend(chunk)
        if len(body)>limit: raise ValueError('source response too large')
    return json.loads(body)

def matches(text, words):
    text = text.casefold()
    for word in words:
        aliases = [word]
        if word.lower() in ['apex','apex英雄']: aliases += ['Apex英雄','Apex Legends']
        if word.lower() in ['lol','英雄联盟']: aliases += ['LOL','英雄联盟']
        if word in ['游戏趣事','有趣日常','二游']: aliases += {'游戏趣事':['游戏'], '有趣日常':['趣事','搞笑','日常'], '二游':['原神','鸣潮','崩坏','绝区零','明日方舟']}[word]
        if any(w.casefold() in text for w in aliases): return True
    return False

from .runtime import link_refs
class Backend:
    def __init__(self, root, config): self.root,self.config=root,config
    async def close(self): pass
    async def search(self, words, max_age=7): return await self.bili_search(words,max_age)
    def refs(self, segments): return link_refs(segments, 'bili')
    async def read(self, value, images=True):
        from .runtime import resolve_bili
        bv=await resolve_bili(value)
        result={'id':bv,'source':'B站','url':'https://www.bilibili.com/video/'+bv,
                'description':'','evidence':'','evidence_kind':'标题与简介','hot_comment':''}
        await self.bili_evidence(result)
        result.update(body=result['evidence'], image_urls=[], images=[], images_total=0, images_loaded=0,
            comment_scope='接口返回的部分热门评论，非全部评论', _detail_loaded=True)
        return result
    async def bili_search(self, words, max_age=7):
        from bilibili_api import search
        out=[]
        today=datetime.now(TZ)
        for word in words:
            data=await asyncio.wait_for(search.search_by_type(word,search.SearchObjectType.VIDEO,
                order_type=search.OrderVideo.TOTALRANK,
                time_start=(today-timedelta(days=max_age)).strftime('%Y-%m-%d'),
                time_end=(today+timedelta(days=1)).strftime('%Y-%m-%d')),25)
            for x in data.get('result',[])[:4]:
                bv=str(x.get('bvid',''))
                if not re.fullmatch(r'BV[0-9A-Za-z]{10}',bv): continue
                stamp=float(x.get('pubdate') or 0)
                out.append(dict(id=bv,source='B站',title=clean(x.get('title'),120),
                    description=clean(x.get('description',''),400),cover=x.get('pic',''),
                    url='https://www.bilibili.com/video/'+bv,published_at=stamp,
                    published=stamp,queries=[word],popularity=x.get('play')))
            await asyncio.sleep(1)
        return out
    async def bili_evidence(self, result):
        from bilibili_api import video,comment,Credential
        from pathlib import Path
        path=self.root/'credentials.json'
        creds=json.loads(path.read_text()) if path.exists() else {}
        credential=Credential(sessdata=creds.get('sessdata') or None,bili_jct=creds.get('bili_jct') or None)
        v=video.Video(bvid=result['id'],credential=credential)
        info=await asyncio.wait_for(v.get_info(),20)
        result['description']=clean(info.get('desc',''),2500)
        result['evidence']=result['description'];result['published_at']=info.get('pubdate')
        result['bili_info']=info
        result['title']=clean(info.get('title',''),200)
        result['cover']=info.get('pic','')
        result['author']=info.get('owner',{}).get('name','')
        try:
            from bilibili_api import user
            relation=await asyncio.wait_for(user.User(info['owner']['mid'],credential=credential).get_relation_info(),8)
            result['bili_fans']=relation.get('follower')
        except Exception: result['bili_fans']=None
        result['comments']=[]
        try:
            data=await asyncio.wait_for(comment.get_comments(info['aid'],comment.CommentResourceType.VIDEO,
                order=comment.OrderType.LIKE,credential=credential),15)
            replies=data.get('hots') or data.get('replies') or []
            result['comments']=[{'author':r.get('member',{}).get('uname','网友'),
                'text':clean(r.get('content',{}).get('message',''),450), 'likes':r.get('like',0)}
                for r in replies[:int(self.config.get('max_comments',12))]]
            if replies:
                top=max(replies,key=lambda x:int(x.get('like') or 0))
                result['hot_comment']=clean(top.get('content',{}).get('message',''),100)
        except Exception: pass
        try:
            cid=(info.get('pages') or [{}])[0].get('cid')
            subs=await asyncio.wait_for(v.get_subtitle(cid),15)
            choices=subs.get('subtitles',[])
            choices.sort(key=lambda x:not x.get('lan','').startswith('zh'))
            if not choices: return
            url=choices[0].get('subtitle_url','')
            if url.startswith('//'):url='https:'+url
            host=urlsplit(url).hostname or ''
            if urlsplit(url).scheme!='https' or not any(host==d or host.endswith('.'+d) for d in ['hdslb.com','bilibili.com','bilivideo.com']): return
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as c:
                async with c.get(url,allow_redirects=False) as response:
                    response.raise_for_status()
                    body=(await bounded_json(response,2*1024*1024)).get('body',[])
            # Spread excerpts over the entire subtitle timeline rather than only the opening.
            if len(body)>80:body=[body[i] for i in sorted({int(j*(len(body)-1)/79) for j in range(80)})]
            text=clean(' '.join(x.get('content','') for x in body),4000)
            if text:result['evidence']=text;result['evidence_kind']='视频字幕节选'
        except Exception: pass
