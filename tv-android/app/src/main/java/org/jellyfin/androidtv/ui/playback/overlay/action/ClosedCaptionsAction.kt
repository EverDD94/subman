package org.jellyfin.androidtv.ui.playback.overlay.action

import android.content.Context
import android.view.View
import android.widget.Toast
import org.jellyfin.androidtv.R
import org.jellyfin.androidtv.ui.everflix.EfPicker
import org.jellyfin.androidtv.ui.playback.PlaybackController
import org.jellyfin.androidtv.ui.playback.overlay.CustomPlaybackTransportControlGlue
import org.jellyfin.androidtv.ui.playback.overlay.VideoPlayerAdapter
import org.jellyfin.androidtv.ui.playback.setSubtitleIndex
import org.jellyfin.sdk.model.api.MediaStreamType
import timber.log.Timber

class ClosedCaptionsAction(
	context: Context,
	customPlaybackTransportControlGlue: CustomPlaybackTransportControlGlue,
) : CustomAction(context, customPlaybackTransportControlGlue) {
	private var popup: EfPicker? = null

	init {
		initializeWithIcon(R.drawable.ic_select_subtitle)
	}

	override fun handleClickAction(
		playbackController: PlaybackController,
		videoPlayerAdapter: VideoPlayerAdapter,
		context: Context,
		view: View,
	) {
		if (playbackController.currentStreamInfo == null) {
			Timber.w("StreamInfo null trying to obtain subtitles")
			Toast.makeText(context, "Unable to obtain subtitle info", Toast.LENGTH_LONG).show()
			return
		}

		videoPlayerAdapter.leanbackOverlayFragment.setFading(false)
		removePopup()
		val subs = playbackController.currentMediaSource.mediaStreams.orEmpty().filter { it.type == MediaStreamType.SUBTITLE }
		val names = subs.map { sub ->
			val text = "${sub.title.orEmpty()} ${sub.displayTitle.orEmpty()}".lowercase()
			val tags = buildList {
				if (sub.isForced || "forced" in text || "forzad" in text) add("Forzados")
				if (sub.isHearingImpaired == true || "sdh" in text) add("SDH")
			}
			val lang = EfPicker.langName(sub.language).ifBlank { if (tags.isEmpty()) "Sin idioma" else "Desconocido" }
			(listOf(lang) + tags).joinToString(" · ")
		}
		val labels = EfPicker.disambiguate(names, names.map { "" })
		val options = listOf(EfPicker.Option("Desactivados", playbackController.subtitleStreamIndex == -1)) +
			subs.mapIndexed { i, sub -> EfPicker.Option(labels[i], sub.index == playbackController.subtitleStreamIndex) }
		popup = EfPicker(
			context, "Subtítulos", options,
			onPick = { i -> playbackController.setSubtitleIndex(if (i == 0) -1 else subs[i - 1].index) },
			onDismiss = {
				videoPlayerAdapter.leanbackOverlayFragment.setFading(true)
				popup = null
			},
		)
		popup?.show()
	}

	fun removePopup() {
		popup?.dismiss()
	}
}
