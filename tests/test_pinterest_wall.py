import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import pinterest_wall as wall


URL = 'https://www.pinterest.com/example/art/'


def pin(number):
    return {'id': str(number), 'type': 'pin', 'image_signature': str(number),
            'images': {'orig': {'url': f'https://i.pinimg.com/originals/{number}.jpg'}}}


def page(pins, bookmark='next'):
    state = {'boards': {'42': {'id': '42', 'url': '/example/art/', 'privacy': 'public'}},
             'resources': {'BoardFeedResource': {
                 json.dumps([['board_id', '42']]): {'data': pins, 'nextBookmark': bookmark}}}}
    return '<script id="__PWS_INITIAL_PROPS__" type="application/json">' + json.dumps({'initialReduxState': state}) + '</script>'


def response(pins, bookmark):
    result = Mock()
    result.json.return_value = {'resource_response': {'status': 'success', 'data': pins, 'bookmark': bookmark}}
    return result


class PinterestTests(unittest.TestCase):
    def test_board_validation(self):
        self.assertEqual(wall.board_url('https://jp.pinterest.com/example/art/'), URL)
        self.assertIn('%E5%8A%A8', wall.board_url('https://jp.pinterest.com/example/动漫/'))
        for url in ('http://www.pinterest.com/example/art/', 'https://evil.test/example/art/',
                    'https://pinterest.com.evil.test/example/art/', 'https://jp.pinterest.com/?tabId=42',
                    'https://www.pinterest.com/pin/42/', 'https://www.pinterest.com/a/b/section/',
                    'https://user:password@www.pinterest.com/a/b/', 'https://www.pinterest.com:8443/a/b/',
                    'https://www.pinterest.com/a/%2e%2e/'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                wall.board_url(url)

    def test_only_requested_public_board(self):
        self.assertEqual(wall.initial_feed(page([pin(1)]), URL)[0]['id'], '42')
        for html in ('<html>Login</html>', page([pin(1)]).replace('public', 'secret'),
                     page([pin(1)]).replace('/example/art/', '/someone/else/')):
            with self.assertRaises(ValueError):
                wall.initial_feed(html, URL)

    def test_paginate_deduplicate_and_stop_at_100(self):
        client = Mock()
        first = Mock(text=page([pin(i) for i in range(25)]))
        client.get.side_effect = [first, response([pin(i) for i in range(20, 70)], 'next2'),
                                 response([pin(i) for i in range(70, 110)], '-end-')]
        board, images = wall.collect(URL, client)
        self.assertEqual(board['id'], '42')
        self.assertEqual(len(images), 100)
        self.assertEqual(len({item['id'] for item in images}), 100)
        self.assertEqual(client.get.call_count, 3)
        options = json.loads(client.get.call_args.kwargs['params']['data'])['options']
        self.assertEqual(options['bookmarks'], ['next2'])

    def test_short_board_and_stalled_pagination(self):
        client = Mock()
        client.get.return_value = Mock(text=page([pin(1), pin(2)], '-end-'))
        self.assertEqual(len(wall.collect(URL, client)[1]), 2)
        for bookmark in (None, 'next'):
            client.get.side_effect = [Mock(text=page([pin(1)])), response([pin(1)], bookmark)]
            with self.assertRaisesRegex(ValueError, 'stalled'):
                wall.collect(URL, client)

    def test_resource_options_bookmark_continues_and_ends_short_board(self):
        client = Mock()
        next_page = response([pin(2)], None)
        next_page.json.return_value['resource'] = {'options': {'bookmarks': ['next2']}}
        last_page = response([pin(3)], None)
        del last_page.json.return_value['resource_response']['bookmark']
        last_page.json.return_value['resource'] = {'options': {'bookmarks': ['-end-']}}
        client.get.side_effect = [Mock(text=page([pin(1)])), next_page, last_page]
        _, images = wall.collect(URL, client)
        self.assertEqual([item['id'] for item in images], ['1', '2', '3'])
        self.assertEqual(client.get.call_count, 3)
        self.assertEqual(json.loads(client.get.call_args.kwargs['params']['data'])['options']['bookmarks'], ['next2'])

    def test_invalid_or_repeated_fallback_bookmark_still_fails(self):
        for bookmarks in ([], ['next'], ['one', 'two'], '-end-', [None]):
            client = Mock()
            last_page = response([pin(2)], None)
            last_page.json.return_value['resource'] = {'options': {'bookmarks': bookmarks}}
            client.get.side_effect = [Mock(text=page([pin(1)])), last_page]
            with self.subTest(bookmarks=bookmarks), self.assertRaisesRegex(ValueError, 'stalled'):
                wall.collect(URL, client)

    def test_untrusted_pin_urls_are_not_downloaded(self):
        for url in ('file:///etc/passwd', 'https://localhost/a.jpg', 'https://evil.test/a.jpg',
                    'https://i.pinimg.com.evil.test/a.jpg', 'https://i.pinimg.com:8443/a.jpg'):
            item = pin(1)
            item['images']['orig']['url'] = url
            self.assertEqual(wall.pin_images(item), [])
        self.assertEqual(wall.pin_images({'type': 'board', 'images': pin(1)['images']}), [])

    def test_mosaic_covers_canvas_without_overlap(self):
        for count in (1, 2, 25, 100):
            for _, width, height, seed in wall.PRESETS:
                tiles = wall.mosaic_layout(count, width, height, random.Random(seed))
                self.assertEqual(len(tiles), count)
                self.assertEqual(sum((r-l)*(b-t) for l,t,r,b in tiles), width*height)
                for i, (l,t,r,b) in enumerate(tiles):
                    self.assertTrue(0 <= l < r <= width and 0 <= t < b <= height)
                    for l2,t2,r2,b2 in tiles[i+1:]:
                        self.assertTrue(r <= l2 or r2 <= l or b <= t2 or b2 <= t)
                self.assertEqual(tiles, wall.mosaic_layout(count, width, height, random.Random(seed)))

    def test_render_uses_every_image_once(self):
        with tempfile.TemporaryDirectory() as directory:
            sources = [Path(directory) / f'{i}.png' for i in range(100)]
            for i, source in enumerate(sources):
                Image.new('RGB', (10, 10), (i*2, 100, 50)).save(source)
            output = Path(directory) / 'wall.jpg'
            with patch('pinterest_wall.Image.open', wraps=Image.open) as opened:
                wall.render(sources, output, 960, 540, 42)
            self.assertEqual(len(opened.call_args_list), 100)
            self.assertEqual({call.args[0] for call in opened.call_args_list}, set(sources))
            with Image.open(output) as image:
                self.assertEqual(image.size, (960, 540))

    def test_download_failure_stops_before_rendering(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('pinterest_wall.collect', return_value=({'id': '42'}, [pin(1)])), \
                patch('pinterest_wall.download', side_effect=ValueError('download failed')), \
                patch('pinterest_wall.render') as render:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, 'download failed'):
                wall.generate(URL, root / 'output', root / 'cache')
            render.assert_not_called()
            self.assertFalse((root / 'output/manifest.json').exists())


if __name__ == '__main__':
    unittest.main()
