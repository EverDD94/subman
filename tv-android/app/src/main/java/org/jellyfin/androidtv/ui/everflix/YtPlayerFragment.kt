package org.jellyfin.androidtv.ui.everflix

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.launch
import androidx.media3.common.MediaItem
import androidx.media3.common.MimeTypes
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.ui.PlayerView
import timber.log.Timber

const val EXTRA_YT_URL = "everflix_yt_url"
const val EXTRA_YT_TITLE = "everflix_yt_title"

/** Plays a YouTube video Videotecario opened (HLS, or a plain MP4 as a last resort). Back goes back. */
class YtPlayerFragment : Fragment() {
	private var player: ExoPlayer? = null

	override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
		val view = PlayerView(requireContext())
		val url = requireArguments().getString(EXTRA_YT_URL).orEmpty()
		val exo = ExoPlayer.Builder(requireContext()).build()
		val item = MediaItem.Builder().setUri(url).apply {
			if (url.contains("m3u8") || url.contains("/yt/")) setMimeType(MimeTypes.APPLICATION_M3U8)
		}.build()
		exo.setMediaItem(item)
		exo.addListener(object : Player.Listener {
			override fun onPlayerError(error: PlaybackException) {
				Timber.w(error, "YouTube")
				activity?.onBackPressedDispatcher?.onBackPressed()
			}

			override fun onPlaybackStateChanged(state: Int) {
				if (state == Player.STATE_ENDED) playNext()
			}
		})
		exo.prepare()
		exo.playWhenReady = true
		view.player = exo
		view.keepScreenOn = true
		view.requestFocus()
		player = exo
		return view
	}

	/** Like YouTube: when a video ends, the next one in the list starts. */
	private fun playNext() {
		val next = YtQueue.videos.getOrNull(YtQueue.index + 1)
		val client = YtQueue.client
		if (next == null || client == null) {
			activity?.onBackPressedDispatcher?.onBackPressed()
			return
		}
		YtQueue.index++
		viewLifecycleOwner.lifecycleScope.launch {
			val stream = client.ytPlay(next)
			val exo = player ?: return@launch
			if (stream == null) { activity?.onBackPressedDispatcher?.onBackPressed(); return@launch }
			exo.setMediaItem(MediaItem.Builder().setUri(stream.url).apply {
				if (stream.url.contains("m3u8") || stream.url.contains("/yt/")) setMimeType(MimeTypes.APPLICATION_M3U8)
			}.build())
			exo.prepare()
			exo.playWhenReady = true
		}
	}

	override fun onPause() {
		super.onPause()
		player?.pause()
	}

	override fun onDestroyView() {
		super.onDestroyView()
		player?.release()
		player = null
	}
}
