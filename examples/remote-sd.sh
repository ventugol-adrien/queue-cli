{
    "definition": \{
        "name": "T2I",
        "description": "Basic text-to-image pipeline executed on local desktop",
        "location": "desktop",
        "type": "text2image"
    },
    "task": \{
        "model": "juggernaut",
        "user_input": "Test",
        "negative_input": "blurry, low quality, artifacts, distorted, watermark",
        "steps": 40,
        "cfg_scale": 7.5,
        "image_seed": -1,
        "batch_size": 1,
        "width": 1024,
        "height": 1024,
        "lightning": false,
        "embedding_mode": "compel",
        "normalize_embeddings": false,
        "prompts": [],
        "loras": [],
        "upscale": 1.0,
        "hi_res_fix": false
    },
    "deliveries": [
        {
            "type": "notification",
            "endpoint": "https://desktop.adriens-apis.io/notify/jobs",
            "title": "Basic Desktop T2I Finished",
            "priority": 3,
            "tags": [
                "art",
                "desktop"
            ],
            "markdown": true
        },
        {
            "type": "email",
            "to": "adrien@example.com",
            "subject": "Generation Complete: Basic Desktop T2I",
            "body": "Your 1024x1024 image generation finished in 40 steps.",
            "attachments": []
        }
    ]
}

PROMPT="a cinematic red fox in a snowy forest"
stable-diffusion --model flux_fp8 --prompt "$PROMPT" --width 1024 --height 1024 --steps 8 --seed 42 --out ~/my_image.png &&
	curl -H "Title: Image Created" -H "X-Target: laptop-adrien" -d "Notification via notify-send" https://desktop.adriens-apis.io/notify/jobs

PROMPT="a cinematic red fox in a snowy forest"
stable-diffusion --model flux_fp8 --prompt "$PROMPT" --width 1024 --height 1024 \ 
--steps 8 --seed 42 --out ~/my_image.png && email -t ventugol.adrien@gmail.com -s "Image Ready" -b "Here is the image of your prompt:$PROMPT" -a ~/my_image.png
