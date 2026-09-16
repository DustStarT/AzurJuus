<script setup lang="ts">
import {
  MessageCircle,
  Aperture,
  Settings,
  Search,
  SlidersHorizontal,
  ChevronLeft,
  ChevronRight,
  Heart,
  Send,
  MoreHorizontal,
  X,
  Check,
  Plus,
  Pause,
  Play,
  Square,
  FolderOpen,
  FileText,
  Activity,
  AlertCircle,
  Users,
  ArrowUpRight,
  Wifi,
  WifiOff,
  ShieldCheck,
  Sparkles,
  Download,
  List,
  Image,
  LoaderCircle,
} from "lucide-vue-next";
const icons = {
  chat: MessageCircle,
  circle: Aperture,
  settings: Settings,
  search: Search,
  filter: SlidersHorizontal,
  back: ChevronLeft,
  next: ChevronRight,
  heart: Heart,
  send: Send,
  more: MoreHorizontal,
  close: X,
  check: Check,
  plus: Plus,
  pause: Pause,
  play: Play,
  stop: Square,
  folder: FolderOpen,
  file: FileText,
  activity: Activity,
  alert: AlertCircle,
  users: Users,
  arrow: ArrowUpRight,
  online: Wifi,
  offline: WifiOff,
  shield: ShieldCheck,
  sparkle: Sparkles,
  download: Download,
  list: List,
  image: Image,
  loading: LoaderCircle,
};
defineProps<{ name: keyof typeof icons; size?: number }>();
</script>
<template>
  <component
    :is="icons[name]"
    :size="size || 21"
    :stroke-width="1.65"
    aria-hidden="true"
  />
</template>
