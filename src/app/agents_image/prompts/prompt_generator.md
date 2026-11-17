# 图生图 Prompt 组装模板（五槽位 + 七条铁律）

[产品身份] {product_block}
[场景与构图] {scene_block}
[光影] {light_block}
[技术参数] {aspect} 比例，{width}x{height}px，背景 {background}，文件 ≤{max_mb}MB，sRGB
[图内文案] {text_block}
[平台预留] {reserve_block}
[否定清单] 无文字水印、无边框、无拼图、无多余物件；保持以下不变：{must_not_change}；禁用表述：{banned}
[写法片段] {rag_block}

补充要求：图位 {slot}（{role}）；用户原始需求：{task_prompt}
