<template>
  <router-view></router-view>
</template>

<script>
import { defineComponent } from 'vue'

export default defineComponent({
  name: 'App'
})
</script>

<style lang="less">
html,
body,
#app {
  width: 100%;
  min-height: 100%;
  margin: 0;
}

body {
  overflow-x: hidden;
}

*,
*::before,
*::after {
  box-sizing: border-box;
}
</style>
