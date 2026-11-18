import io
import os
import unittest
from unittest.mock import patch

from PIL import Image
from fastapi.testclient import TestClient
import app
from football_card import render_card


class FootballCardTests(unittest.TestCase):
    def test_round_flags_do_not_turn_transparent_padding_into_black_bands(self):
        badge=Image.new('RGBA',(300,300))
        from PIL import ImageDraw
        ImageDraw.Draw(badge).rectangle((0,70,299,229),fill=(240,30,30,255))
        data={'status':'Finished','teams':[{'name':'Home','score':0,'round_badge':True},{'name':'Away','score':0}]}
        result=Image.open(io.BytesIO(render_card(data,(badge,None))))
        self.assertEqual(result.getpixel((180,60)),(240,30,30))

    def test_endpoint_requires_auth_and_returns_real_png_without_external_badges(self):
        payload={'status':'Half-time','competition':'Test','teams':[
            {'name':'Wales','score':0,'badge':'https://127.0.0.1/private'},
            {'name':'Portugal','score':0,'badge':'https://evil.example/image.png'}]}
        with patch.dict(os.environ,{'MEDIA_BACKEND_SECRET':'s'*40}),patch.object(app,'fetch_file') as download:
            client=TestClient(app.app)
            self.assertEqual(client.post('/football-card',json=payload).status_code,401)
            result=client.post('/football-card',json=payload,headers={'Authorization':'Bearer '+'s'*40})
            self.assertEqual(result.status_code,200)
            image=Image.open(io.BytesIO(result.content));self.assertEqual(image.size,(1280,720))
            self.assertEqual(image.format,'PNG');download.assert_not_called()
            payload['teams'][0]['score']=-1
            self.assertEqual(client.post('/football-card',json=payload,headers={'Authorization':'Bearer '+'s'*40}).status_code,422)

    def test_large_scores_assists_unicode_and_missing_badges_fit_canvas(self):
        goals=[{'player':'Álex '+str(n),'minute':"90+12'",'assist':'João Félix'} for n in range(12)]
        data={'status':'Finished','penalties':'5–4','teams':[
            {'name':'A very long international team name','score':12,'goals':goals,'cards':['Red: Player 42\'']},
            {'name':'Other team','score':0,'goals':[]}]}
        image=Image.open(io.BytesIO(render_card(data)))
        self.assertGreaterEqual(image.height,375+12*88+100)
        self.assertLess(len(render_card(data)),5*1024*1024)


if __name__=='__main__':unittest.main()
