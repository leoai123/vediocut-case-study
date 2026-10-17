# footage/

把要剪的素材放在這裡，一個資料夾一支片：

```
footage/
└── myvlog/
    ├── IMG_0001.MOV
    ├── IMG_0002.MOV
    └── bgm.mp3        ← 選用的配樂（一到兩首）
```

這個資料夾的內容不會進版控（見 `.gitignore`）。

**注意**：檔名不能有空格（macOS Finder 複製會產生 `IMG_0001 2.MOV`）。
`scripts/doctor.sh` 與 skill 的第 1 步都會檢查。
