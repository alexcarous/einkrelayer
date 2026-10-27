"""Reader styles and deterministic covers for einkrelayer."""

CSS = """body{font-family:serif}
.math-inline{width:auto;vertical-align:middle}.math-display{display:block;max-width:100%;margin:1em auto;width:auto}
.plain-text{font-family:inherit}
.heading-lead{page-break-inside:avoid;break-inside:avoid}
section,article,main,header,footer,aside,figure,figcaption{display:block}
h1,h2,h3,h4{text-align:left;page-break-after:avoid;break-after:avoid}
h1{font-size:1.65em;margin:0 0 1em}h2{font-size:1.25em;margin:1.5em 0 .65em}
h3{font-size:1.08em;margin:1.25em 0 .5em}
p{margin:.55em 0;text-indent:0}li{margin:.35em 0}
ul,ol{padding-left:6%;margin:.65em 0}blockquote{margin:1em 4%}
ol[type="A"]{list-style-type:upper-alpha}.exercise-list{list-style-type:none;padding-left:0}
img{max-width:100%;height:auto}figure{margin:1.1em 0;text-align:center;page-break-inside:avoid;break-inside:avoid}
figure img{width:100%;object-fit:contain}figcaption,.figure-caption{font-size:.9em;text-align:left;margin:.45em 0}
table{border-collapse:collapse;width:100%;margin:1em 0}
td,th{text-align:left;vertical-align:top;border-bottom:1px solid;padding:.45em .5em;overflow-wrap:break-word}
thead{display:table-header-group}tr{page-break-inside:avoid}
dl{margin:.8em 0}dt{font-weight:bold;margin:1em 0 .25em;page-break-after:avoid}dd{margin:0 0 .7em}
pre{white-space:pre-wrap}code,pre,a{overflow-wrap:break-word;word-wrap:break-word}
.source-note,.chapter-links{font-size:.85em}.source-note{margin-bottom:1.25em}
.chapter-links{margin:1.25em 0}.source-label{font-family:sans-serif;font-size:.8em;letter-spacing:.1em;margin:0 0 1em}
.answer-key-heading{page-break-before:always;break-before:page}.answer{margin-bottom:1.5em}
.titlepage{text-align:center}.titlepage h1{text-align:center;font-size:2.1em;margin:2em 0 .8em}
.titlepage .subtitle{font-size:1.2em;margin:0 0 2em}.titlepage .contributors{margin:2em 0}
.front-note{font-size:.9em}.cover-page{margin:0;padding:0;text-align:center}
.cover-page p{margin:0}.cover-page img{width:100%;max-width:100%;max-height:100%;object-fit:contain}
.contents ol{list-style-type:none;padding-left:0}.contents ol ol{padding-left:5%;margin-bottom:1em}
.contents .source-group>a{font-weight:bold}.contents h1{margin-bottom:.8em}
.chapter-contents{margin:1em 0 1.5em}.chapter-contents ul{list-style-type:none;padding-left:0}
nav[hidden]{display:none}
.record{margin:1em 0}.record h3{margin-bottom:.35em}
"""
