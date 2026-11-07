"""练习页：改谱的乐观锁（JSON 编辑器、撤销、同一小节连着改）和通知区的顺序。"""

import json
import time
import urllib.request

import pytest

pytestmark = pytest.mark.e2e


def _song(server, sid):
    return json.load(urllib.request.urlopen(f"{server['base']}/api/songs/{sid}"))


def test_notices_keep_order_regardless_of_arrival(trainer):
    b = trainer()
    order = b.js("""import('/static/js/trainer/notices.js').then(({ notice }) => {
      const ids = ['progress', 'offline', 'error', 'resume', 'explain', 'metaNote'];
      ids.forEach(id => notice(id, null));
      for (const id of ['metaNote', 'resume', 'offline', 'explain', 'progress']) notice(id, { kind: 'info', html: id });
      const got = [...document.getElementById('notices').children].map(x => x.dataset.id);
      ids.forEach(id => notice(id, null));
      return got;
    })""")
    assert order == ["progress", "offline", "resume", "explain", "metaNote"]
    assert not b.errors, b.errors
