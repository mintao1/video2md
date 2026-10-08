import { computed, ref, onUnmounted } from 'vue'

/** 验证码发送倒计时（60s） */
export function useCountdown(seconds = 60) {
  const remaining = ref(0)
  let timer: ReturnType<typeof setInterval> | null = null

  const running = computed(() => remaining.value > 0)

  function start() {
    if (remaining.value > 0) return
    remaining.value = seconds
    timer = setInterval(() => {
      remaining.value -= 1
      if (remaining.value <= 0) stopTimer()
    }, 1000)
  }

  function stopTimer() {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }

  onUnmounted(stopTimer)

  return { remaining, running, start }
}
