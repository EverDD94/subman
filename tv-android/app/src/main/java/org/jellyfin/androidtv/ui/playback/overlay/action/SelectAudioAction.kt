package org.jellyfin.androidtv.ui.playback.overlay.action

import android.content.Context
import android.view.View
import org.jellyfin.androidtv.R
import org.jellyfin.androidtv.ui.everflix.EfPicker
import org.jellyfin.androidtv.ui.playback.PlaybackController
import org.jellyfin.androidtv.ui.playback.overlay.CustomPlaybackTransportControlGlue
import org.jellyfin.androidtv.ui.playback.overlay.VideoPlayerAdapter

class SelectAudioAction(
	context: Context,
	customPlaybackTransportControlGlue: CustomPlaybackTransportControlGlue,
) : CustomAction(context, customPlaybackTransportControlGlue) {
	private var popup: EfPicker? = null

	init {
		initializeWithIcon(R.drawable.ic_select_audio)
	}

	override fun handleClickAction(
		playbackController: PlaybackController,
		videoPlayerAdapter: VideoPlayerAdapter,
		context: Context,
		view: View,
	) {
		videoPlayerAdapter.leanbackOverlayFragment.setFading(false)
		val audioTracks = playbackController.currentStreamInfo?.selectableAudioStreams ?: return
		val currentAudioIndex = playbackController.audioStreamIndex

		dismissPopup()
		val names = audioTracks.mapIndexed { i, t -> EfPicker.langName(t.language).ifBlank { "Pista ${i + 1}" } }
		val labels = EfPicker.disambiguate(names, audioTracks.map { EfPicker.channels(it.channels) })
		popup = EfPicker(
			context, "Audio",
			audioTracks.mapIndexed { i, t -> EfPicker.Option(labels[i], currentAudioIndex == t.index) },
			onPick = { i -> playbackController.switchAudioStream(audioTracks[i].index) },
			onDismiss = {
				videoPlayerAdapter.leanbackOverlayFragment.setFading(true)
				popup = null
			},
		)
		popup?.show()
	}

	fun dismissPopup() {
		popup?.dismiss()
	}
}
