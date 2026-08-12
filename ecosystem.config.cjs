{
  "apps": [
    {
      "name": "leady-sprzedawcy",
      "script": "start.sh",
      "interpreter": "bash",
      "cwd": ".",
      "env": {
        "PORT": "8000"
      },
      "max_restarts": 10,
      "restart_delay": 5000
    }
  ]
}
