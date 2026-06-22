import re

ARTICLE_INJECT_CSS = r"""
/* injected by wechat_image_style.py */
#page-root{ margin:0 auto; }

/*
目标：
1) 正文左对齐（不两端对齐）
2) 不动现有 #page-root padding
3) 限制内容块宽度 ≈ 19 个中文字符/行
4) 内容块居中 -> 左右留白相等
*/
:root{
  --wx-line-chars: 19;
  --wx-body-letter-spacing: 2.25px; /* 需与你模板 .content p 的 letter-spacing 保持一致 */
  --wx-content-width: calc(var(--wx-line-chars) * (var(--body-size) + var(--wx-body-letter-spacing)));
}

/* 把“内容块”做窄并居中：标题区、封面图、正文都跟着同一宽度走 */
.title-row,
.cover,
.content{
  width: min(var(--column-width), var(--wx-content-width));
  margin-left: auto;
  margin-right: auto;
}

/* 正文左对齐 */
.content p{
  text-align: left;
  text-align-last: left;
  text-justify: auto;
}
"""

REFERENCE_INJECT_CSS = r"""
/* injected by wechat_image_style.py */
#page-root{ margin:0 auto; }
ol,li{ text-align:left; }
"""

def _inject_before_style_close(template_html: str, css: str) -> str:
    if "</style>" not in template_html:
        return template_html
    return re.sub(r"</style>", css + "\n</style>", template_html, count=1)

def patch_wechat_template(template_html: str, kind: str) -> str:
    k = (kind or "").lower().strip()
    css = ARTICLE_INJECT_CSS if k == "article" else REFERENCE_INJECT_CSS
    return _inject_before_style_close(template_html, css)
